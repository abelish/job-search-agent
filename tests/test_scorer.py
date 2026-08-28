"""
Tests for agents/scorer — passes_hard_filters, _fmt_profile, score_posting, score_all.
Claude calls are mocked; all hard-filter logic is deterministic and tested directly.
"""

import pytest
from unittest.mock import patch, call

from agents.scorer import (
    passes_hard_filters,
    hard_filter_reason,
    _fmt_profile,
    _extract_json,
    _is_director_plus,
    score_posting,
    score_all,
    reapply_adjustments,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

PROFILE = {
    "name": "Test User",
    "current_title": "Director, Engineering",
    "target_titles": ["VP Engineering", "Director of Engineering"],
    "years_experience": 20,
    "core_skills": ["Python", "AWS"],
    "domains": ["SaaS", "platform engineering"],
    "comp_floor_usd": 200000,
    "locations": {
        "remote_ok": True,
        "city_allowlist_international": ["Berlin"],
        "preferred_regions_us": ["San Francisco Bay Area"],
        "willing_to_relocate": True,
    },
    "exclude_companies": ["BadCorp", "WorstCo"],
    "must_have_keywords": [],
    "require_english_workplace": False,
    "require_visa_sponsorship_for_international_roles": False,
    "notes": "",
}

# PROFILE has no priority_location_terms configured, so it gets no location-based
# scoring adjustment at all (see test_score_posting_no_location_adjustment_when_not_configured).
# LOCATION_PROFILE opts into the adjustment, for tests that exercise it directly.
LOCATION_PROFILE = {
    **PROFILE,
    "locations": {
        **PROFILE["locations"],
        "priority_location_terms": ["san francisco", "bay area"],
        "priority_location_boost": 8,
        "non_priority_manager_penalty": 10,
    },
}

# Outside LOCATION_PROFILE's priority terms, so score tests don't pick up its boost.
POSTING = {
    "id": "abc123def456",
    "source": "greenhouse",
    "title": "VP Engineering",
    "company": "GoodCorp",
    "location": "New York, NY",
    "url": "https://example.com/job/1",
    "description": "We are looking for a VP of Engineering to lead our platform team.",
    "posted_date": "2026-06-01",
    "fetched_date": "2026-06-24T00:00:00Z",
}

CLAUDE_RESPONSE = {
    "text": '{"score": 85, "rationale": "Strong match on seniority and domain experience"}',
    "input_tokens": 500,
    "output_tokens": 80,
    "model": "claude-sonnet-4-6",
}


# ---------------------------------------------------------------------------
# passes_hard_filters — exclude_companies
# ---------------------------------------------------------------------------

def test_passes_basic_posting():
    assert passes_hard_filters(POSTING, PROFILE) is True


def test_excludes_company_exact():
    assert passes_hard_filters({**POSTING, "company": "BadCorp"}, PROFILE) is False


def test_excludes_company_case_insensitive():
    assert passes_hard_filters({**POSTING, "company": "badcorp"}, PROFILE) is False
    assert passes_hard_filters({**POSTING, "company": "WORSTCO"}, PROFILE) is False


def test_non_excluded_company_passes():
    assert passes_hard_filters({**POSTING, "company": "AcmeCorp"}, PROFILE) is True


# ---------------------------------------------------------------------------
# passes_hard_filters — exclude_title_keywords
# ---------------------------------------------------------------------------

def test_exclude_title_keyword_blocks_matching_title():
    profile = {**PROFILE, "exclude_title_keywords": ["account director"]}
    assert passes_hard_filters({**POSTING, "title": "Account Director"}, profile) is False

def test_exclude_title_keyword_case_insensitive():
    profile = {**PROFILE, "exclude_title_keywords": ["account manager"]}
    assert passes_hard_filters({**POSTING, "title": "ACCOUNT MANAGER, Enterprise"}, profile) is False

def test_exclude_title_keyword_substring_match():
    profile = {**PROFILE, "exclude_title_keywords": ["account director"]}
    assert passes_hard_filters({**POSTING, "title": "Senior Account Director, EMEA"}, profile) is False

def test_exclude_title_keyword_does_not_block_non_matching():
    profile = {**PROFILE, "exclude_title_keywords": ["account director", "account manager"]}
    assert passes_hard_filters({**POSTING, "title": "VP Engineering"}, profile) is True

def test_exclude_title_keyword_empty_list_passes_all():
    profile = {**PROFILE, "exclude_title_keywords": []}
    assert passes_hard_filters(POSTING, profile) is True


# ---------------------------------------------------------------------------
# passes_hard_filters — must_have_keywords
# ---------------------------------------------------------------------------

def test_must_have_keywords_all_present():
    profile = {**PROFILE, "must_have_keywords": ["engineering", "platform"]}
    assert passes_hard_filters(POSTING, profile) is True


def test_must_have_keywords_one_missing():
    profile = {**PROFILE, "must_have_keywords": ["engineering", "blockchain"]}
    assert passes_hard_filters(POSTING, profile) is False


def test_must_have_keywords_checks_title_and_description():
    profile = {**PROFILE, "must_have_keywords": ["VP"]}
    assert passes_hard_filters(POSTING, profile) is True


def test_must_have_keywords_case_insensitive():
    profile = {**PROFILE, "must_have_keywords": ["PLATFORM"]}
    assert passes_hard_filters(POSTING, profile) is True


# ---------------------------------------------------------------------------
# passes_hard_filters — must_have_title_keywords (OR logic, title only)
# ---------------------------------------------------------------------------

def test_title_keyword_match_passes():
    profile = {**PROFILE, "must_have_title_keywords": ["manager", "director"]}
    assert passes_hard_filters({**POSTING, "title": "Engineering Manager"}, profile) is True

def test_title_keyword_any_one_passes():
    profile = {**PROFILE, "must_have_title_keywords": ["manager", "director", "vp"]}
    assert passes_hard_filters({**POSTING, "title": "VP Engineering"}, profile) is True

def test_title_keyword_none_match_fails():
    profile = {**PROFILE, "must_have_title_keywords": ["manager", "director", "vp"]}
    assert passes_hard_filters({**POSTING, "title": "Account Executive"}, profile) is False

def test_title_keyword_not_in_description_only():
    profile = {**PROFILE, "must_have_title_keywords": ["manager"]}
    posting = {**POSTING, "title": "Senior Software Engineer", "description": "Report to engineering manager"}
    assert passes_hard_filters(posting, profile) is False

def test_title_keyword_case_insensitive():
    profile = {**PROFILE, "must_have_title_keywords": ["VP"]}
    assert passes_hard_filters({**POSTING, "title": "vp engineering"}, profile) is True

def test_title_keyword_empty_list_passes_all():
    assert passes_hard_filters(POSTING, PROFILE) is True

def test_title_keyword_head_of_match():
    profile = {**PROFILE, "must_have_title_keywords": ["head of"]}
    assert passes_hard_filters({**POSTING, "title": "Head of Engineering"}, profile) is True


# ---------------------------------------------------------------------------
# passes_hard_filters — location (US sources)
# ---------------------------------------------------------------------------

def test_remote_passes_when_remote_ok():
    assert passes_hard_filters({**POSTING, "location": "Remote"}, PROFILE) is True


def test_remote_fails_when_not_remote_ok():
    profile = {**PROFILE, "locations": {**PROFILE["locations"], "remote_ok": False}}
    assert passes_hard_filters({**POSTING, "location": "Remote"}, profile) is False


def test_us_city_passes_when_willing_to_relocate():
    assert passes_hard_filters({**POSTING, "location": "New York, NY"}, PROFILE) is True


def test_us_city_fails_when_not_willing_to_relocate_and_not_in_allowlist():
    profile = {**PROFILE, "locations": {**PROFILE["locations"], "willing_to_relocate": False}}
    assert passes_hard_filters({**POSTING, "location": "New York, NY"}, profile) is False


def test_empty_location_passes():
    assert passes_hard_filters({**POSTING, "location": ""}, PROFILE) is True


# ---------------------------------------------------------------------------
# passes_hard_filters — city_allowlist_international (German sources)
# ---------------------------------------------------------------------------

def test_german_source_berlin_passes():
    posting = {**POSTING, "source": "arbeitsagentur", "location": "Berlin, Germany"}
    assert passes_hard_filters(posting, PROFILE) is True


def test_german_source_non_berlin_blocked():
    posting = {**POSTING, "source": "arbeitsagentur", "location": "München"}
    assert passes_hard_filters(posting, PROFILE) is False


def test_german_source_hamburg_blocked():
    posting = {**POSTING, "source": "smartrecruiters", "location": "Hamburg, Germany"}
    assert passes_hard_filters(posting, PROFILE) is False


def test_german_source_remote_passes():
    posting = {**POSTING, "source": "arbeitsagentur", "location": "Remote"}
    assert passes_hard_filters(posting, PROFILE) is True


def test_us_source_non_berlin_not_blocked_by_allowlist():
    posting = {**POSTING, "source": "greenhouse", "location": "Munich, Germany"}
    assert passes_hard_filters(posting, PROFILE) is True


# ---------------------------------------------------------------------------
# passes_hard_filters — require_english_workplace
# ---------------------------------------------------------------------------

GERMAN_DESC = (
    "Wir suchen einen erfahrenen Ingenieur. Ihre Aufgaben umfassen die Entwicklung "
    "von Softwarelösungen. Du bringst mit: Kenntnisse in Python. Was wir bieten: "
    "ein tolles Team und gute Bezahlung."
)

def test_english_workplace_blocks_german_posting():
    profile = {**PROFILE, "require_english_workplace": True}
    posting = {**POSTING, "description": GERMAN_DESC}
    assert passes_hard_filters(posting, profile) is False


def test_english_workplace_passes_english_posting():
    profile = {**PROFILE, "require_english_workplace": True}
    assert passes_hard_filters(POSTING, profile) is True


def test_english_workplace_off_does_not_filter_german():
    assert passes_hard_filters({**POSTING, "description": GERMAN_DESC}, PROFILE) is True


def test_english_workplace_blocks_explicit_german_required_on_german_source():
    profile = {**PROFILE, "require_english_workplace": True}
    posting = {
        **POSTING,
        "source": "smartrecruiters",
        "location": "Berlin, Germany",
        "description": "Great VP Engineering role in Berlin. German required for all internal communications.",
    }
    assert passes_hard_filters(posting, profile) is False


def test_english_workplace_deutsch_erforderlich_blocked():
    profile = {**PROFILE, "require_english_workplace": True}
    posting = {
        **POSTING,
        "source": "arbeitsagentur",
        "location": "Berlin",
        "description": "Engineering role. Deutsch erforderlich.",
    }
    assert passes_hard_filters(posting, profile) is False


def test_english_workplace_explicit_german_only_applies_to_german_sources():
    profile = {**PROFILE, "require_english_workplace": True}
    posting = {
        **POSTING,
        "source": "greenhouse",
        "location": "San Francisco",
        "description": "Great role. German required is a bonus but not mandatory.",
    }
    assert passes_hard_filters(posting, profile) is True


# ---------------------------------------------------------------------------
# _fmt_profile — prompt content
# ---------------------------------------------------------------------------

def test_fmt_profile_includes_name():
    assert "Test User" in _fmt_profile(PROFILE)


def test_fmt_profile_includes_target_titles():
    result = _fmt_profile(PROFILE)
    assert "VP Engineering" in result
    assert "Director of Engineering" in result


def test_fmt_profile_includes_comp_floor():
    assert "200,000" in _fmt_profile(PROFILE)


def test_fmt_profile_includes_domains():
    result = _fmt_profile(PROFILE)
    assert "SaaS" in result
    assert "platform engineering" in result


def test_fmt_profile_includes_city_allowlist():
    assert "Berlin" in _fmt_profile(PROFILE)


def test_fmt_profile_includes_preferred_regions():
    assert "San Francisco Bay Area" in _fmt_profile(PROFILE)


def test_fmt_profile_english_workplace_requirement():
    profile = {**PROFILE, "require_english_workplace": True}
    result = _fmt_profile(profile)
    assert "English" in result
    assert "working language" in result


def test_fmt_profile_visa_requirement():
    profile = {**PROFILE, "require_visa_sponsorship_for_international_roles": True}
    result = _fmt_profile(profile)
    assert "visa" in result.lower()
    assert "United States" in result


def test_fmt_profile_exclude_companies():
    result = _fmt_profile(PROFILE)
    assert "BadCorp" in result
    assert "WorstCo" in result


def test_fmt_profile_industry_favor():
    profile = {**PROFILE, "industry_preferences": {"favor": ["climate change", "hunger relief"]}}
    result = _fmt_profile(profile)
    assert "Favor companies" in result
    assert "climate change" in result
    assert "hunger relief" in result


def test_fmt_profile_industry_avoid():
    profile = {**PROFILE, "industry_preferences": {"avoid": ["oil and gas", "defense and weapons"]}}
    result = _fmt_profile(profile)
    assert "Avoid companies" in result
    assert "oil and gas" in result
    assert "defense and weapons" in result


def test_fmt_profile_omits_industry_preferences_when_absent():
    result = _fmt_profile(PROFILE)
    assert "Favor companies" not in result
    assert "Avoid companies" not in result


def test_fmt_profile_omits_empty_fields():
    profile = {**PROFILE, "notes": "", "core_skills": []}
    result = _fmt_profile(profile)
    assert "Additional notes" not in result
    assert "Core skills" not in result


# ---------------------------------------------------------------------------
# _extract_json — code fence stripping
# ---------------------------------------------------------------------------

def test_extract_json_plain():
    assert _extract_json('{"score": 80}') == '{"score": 80}'


def test_extract_json_strips_json_fence():
    fenced = '```json\n{"score": 80}\n```'
    assert _extract_json(fenced) == '{"score": 80}'


def test_extract_json_strips_plain_fence():
    fenced = '```\n{"score": 80}\n```'
    assert _extract_json(fenced) == '{"score": 80}'


def test_extract_json_strips_whitespace():
    assert _extract_json('  {"score": 80}  ') == '{"score": 80}'


# ---------------------------------------------------------------------------
# score_posting — Claude response parsing
# ---------------------------------------------------------------------------

def test_score_posting_parses_valid_response():
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        result = score_posting(POSTING, PROFILE)
    assert result["fit_score"] == 85
    assert "Strong match" in result["fit_rationale"]


def test_score_posting_no_location_adjustment_when_not_configured():
    # PROFILE has no priority_location_terms — no boost/penalty should apply, ever.
    sf_posting = {**POSTING, "location": "San Francisco, CA"}
    manager_posting = {**POSTING, "title": "Engineering Manager"}
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        sf_result = score_posting(sf_posting, PROFILE)
        manager_result = score_posting(manager_posting, PROFILE)
    assert sf_result["fit_score"] == 85
    assert manager_result["fit_score"] == 85
    assert "(" not in sf_result["fit_rationale"]
    assert "(" not in manager_result["fit_rationale"]


def test_score_posting_applies_priority_location_boost():
    sf_posting = {**POSTING, "location": "San Francisco, CA"}
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        result = score_posting(sf_posting, LOCATION_PROFILE)
    assert result["fit_score"] == 93  # 85 + 8 boost
    assert "Priority location boost" in result["fit_rationale"]


def test_score_posting_priority_location_boost_caps_at_100():
    high_response = {**CLAUDE_RESPONSE, "text": '{"score": 97, "rationale": "Near perfect"}'}
    sf_posting = {**POSTING, "location": "San Francisco, CA"}
    with patch("agents.scorer.complete", return_value=high_response):
        result = score_posting(sf_posting, LOCATION_PROFILE)
    assert result["fit_score"] == 100


def test_score_posting_preserves_posting_fields():
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        result = score_posting(POSTING, PROFILE)
    assert result["id"] == POSTING["id"]
    assert result["company"] == POSTING["company"]


def test_score_posting_includes_token_fields_for_aggregation():
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        result = score_posting(POSTING, PROFILE)
    assert result["_input_tokens"] == 500
    assert result["_output_tokens"] == 80
    assert result["_model"] == "claude-sonnet-4-6"


def test_score_posting_handles_malformed_json():
    bad_response = {**CLAUDE_RESPONSE, "text": "not valid json at all"}
    with patch("agents.scorer.complete", return_value=bad_response):
        result = score_posting(POSTING, PROFILE)
    assert result["fit_score"] == 0
    assert "Parse error" in result["fit_rationale"]


def test_score_posting_handles_code_fenced_json():
    fenced = {**CLAUDE_RESPONSE, "text": '```json\n{"score": 72, "rationale": "Good fit"}\n```'}
    with patch("agents.scorer.complete", return_value=fenced):
        result = score_posting(POSTING, PROFILE)
    assert result["fit_score"] == 72


def test_score_posting_retries_on_empty_response():
    empty = {**CLAUDE_RESPONSE, "text": ""}
    with patch("agents.scorer.complete", side_effect=[empty, CLAUDE_RESPONSE]):
        result = score_posting(POSTING, PROFILE)
    assert result["fit_score"] == 85


# ---------------------------------------------------------------------------
# _is_director_plus — title-level seniority detection
# ---------------------------------------------------------------------------

def test_is_director_plus_vp():
    assert _is_director_plus("VP Engineering") is True

def test_is_director_plus_director():
    assert _is_director_plus("Director of Engineering") is True

def test_is_director_plus_head_of():
    assert _is_director_plus("Head of Engineering") is True

def test_is_director_plus_cto():
    assert _is_director_plus("CTO") is True

def test_is_director_plus_chief():
    assert _is_director_plus("Chief Technology Officer") is True

def test_is_director_plus_engineering_manager():
    assert _is_director_plus("Engineering Manager") is False

def test_is_director_plus_senior_engineering_manager():
    assert _is_director_plus("Senior Engineering Manager") is False

def test_is_director_plus_case_insensitive():
    assert _is_director_plus("vp of engineering") is True
    assert _is_director_plus("engineering manager") is False


# ---------------------------------------------------------------------------
# score_posting — non-priority manager-level penalty (opt-in, profile configured)
# ---------------------------------------------------------------------------

def test_score_posting_applies_non_priority_manager_penalty():
    manager_posting = {**POSTING, "title": "Engineering Manager"}
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        result = score_posting(manager_posting, LOCATION_PROFILE)
    assert result["fit_score"] == 75  # 85 - 10 penalty
    assert "Non-priority manager-level penalty" in result["fit_rationale"]


def test_score_posting_no_penalty_for_director_outside_priority_location():
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        result = score_posting(POSTING, LOCATION_PROFILE)  # VP Engineering in New York — director+, no adjustment
    assert result["fit_score"] == 85


def test_score_posting_no_penalty_when_priority_location_manager():
    manager_sf = {**POSTING, "title": "Engineering Manager", "location": "San Francisco, CA"}
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        result = score_posting(manager_sf, LOCATION_PROFILE)
    assert result["fit_score"] == 93  # 85 + 8 priority location boost, no penalty
    assert "Priority location boost" in result["fit_rationale"]
    assert "penalty" not in result["fit_rationale"]


def test_score_posting_non_priority_manager_penalty_floors_at_zero():
    very_low = {**CLAUDE_RESPONSE, "text": '{"score": 5, "rationale": "Very poor fit"}'}
    manager_posting = {**POSTING, "title": "Engineering Manager"}
    with patch("agents.scorer.complete", return_value=very_low):
        result = score_posting(manager_posting, LOCATION_PROFILE)
    assert result["fit_score"] == 0  # floor at 0, not negative


def test_score_posting_penalty_ignored_when_zero_even_with_terms_set():
    profile = {**PROFILE, "locations": {**PROFILE["locations"],
                                         "priority_location_terms": ["san francisco"],
                                         "priority_location_boost": 8,
                                         "non_priority_manager_penalty": 0}}
    manager_posting = {**POSTING, "title": "Engineering Manager"}
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        result = score_posting(manager_posting, profile)
    assert result["fit_score"] == 85
    assert "(" not in result["fit_rationale"]


# ---------------------------------------------------------------------------
# score_all — tuple return, threshold, hard filter, sort, callbacks
# ---------------------------------------------------------------------------

def test_score_all_returns_tuple():
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        with patch("agents.scorer.log_activity"):
            result = score_all([POSTING], PROFILE, threshold=70)
    assert isinstance(result, tuple)
    assert len(result) == 3


def test_score_all_strips_token_fields_from_output():
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        with patch("agents.scorer.log_activity"):
            above, below, filtered = score_all([POSTING], PROFILE, threshold=70)
    assert "_input_tokens" not in above[0]
    assert "_output_tokens" not in above[0]
    assert "_model" not in above[0]


def test_score_all_returns_above_threshold():
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        with patch("agents.scorer.log_activity"):
            above, below, filtered = score_all([POSTING], PROFILE, threshold=70)
    assert len(above) == 1
    assert above[0]["fit_score"] == 85
    assert len(below) == 0


def test_score_all_below_threshold_goes_to_below_list():
    low_response = {**CLAUDE_RESPONSE, "text": '{"score": 40, "rationale": "Poor fit"}'}
    with patch("agents.scorer.complete", return_value=low_response):
        with patch("agents.scorer.log_activity"):
            above, below, filtered = score_all([POSTING], PROFILE, threshold=70)
    assert above == []
    assert len(below) == 1
    assert below[0]["fit_score"] == 40
    assert below[0]["fit_rationale"] == "Poor fit"


def test_score_all_below_threshold_keeps_real_rationale():
    low_response = {**CLAUDE_RESPONSE, "text": '{"score": 55, "rationale": "Missing cloud experience"}'}
    with patch("agents.scorer.complete", return_value=low_response):
        with patch("agents.scorer.log_activity"):
            above, below, filtered = score_all([POSTING], PROFILE, threshold=70)
    assert below[0]["fit_rationale"] == "Missing cloud experience"


def test_score_all_hard_filter_failures_go_to_filtered_list():
    excluded_posting = {**POSTING, "company": "BadCorp"}
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE) as mock_claude:
        with patch("agents.scorer.log_activity"):
            above, below, filtered = score_all([excluded_posting], PROFILE)
    mock_claude.assert_not_called()
    assert above == []
    assert below == []
    assert len(filtered) == 1
    assert filtered[0]["fit_score"] is None
    assert "BadCorp" in filtered[0]["fit_rationale"]
    assert filtered[0]["fit_rationale"].startswith("Filtered out:")


def test_score_all_sorts_by_score_descending():
    postings = [
        {**POSTING, "id": "a", "title": "VP Engineering"},
        {**POSTING, "id": "b", "title": "VP Engineering"},
    ]
    responses = [
        {**CLAUDE_RESPONSE, "text": '{"score": 75, "rationale": "OK"}'},
        {**CLAUDE_RESPONSE, "text": '{"score": 90, "rationale": "Great"}'},
    ]
    with patch("agents.scorer.complete", side_effect=responses):
        with patch("agents.scorer.log_activity"):
            above, below, filtered = score_all(postings, PROFILE, threshold=70)
    assert above[0]["fit_score"] == 90
    assert above[1]["fit_score"] == 75


def test_score_all_on_progress_called_per_job():
    postings = [
        {**POSTING, "id": "a"},
        {**POSTING, "id": "b"},
    ]
    calls = []
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        with patch("agents.scorer.log_activity"):
            score_all(postings, PROFILE, on_progress=lambda: calls.append(1))
    assert len(calls) == 2


def test_score_all_on_progress_called_for_hard_filtered():
    excluded = {**POSTING, "company": "BadCorp"}
    normal = {**POSTING, "id": "b"}
    calls = []
    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        with patch("agents.scorer.log_activity"):
            score_all([excluded, normal], PROFILE, on_progress=lambda: calls.append(1))
    assert len(calls) == 2  # one for filtered, one for scored


def test_score_all_should_stop_halts_loop():
    postings = [
        {**POSTING, "id": "a"},
        {**POSTING, "id": "b"},
        {**POSTING, "id": "c"},
    ]
    stop_after = [0]

    def _should_stop():
        stop_after[0] += 1
        return stop_after[0] > 1  # stop after first job

    with patch("agents.scorer.complete", return_value=CLAUDE_RESPONSE):
        with patch("agents.scorer.log_activity"):
            above, below, filtered = score_all(postings, PROFILE, should_stop=_should_stop)
    # Only 1 job should have been scored before stop

    assert len(above) + len(below) == 1


# ---------------------------------------------------------------------------
# reapply_adjustments — re-derive location/seniority adjustment without Claude
# ---------------------------------------------------------------------------

def test_reapply_adjustments_no_change_when_profile_unconfigured():
    scored = [{**POSTING, "fit_score": 85, "fit_rationale": "Strong match"}]
    assert reapply_adjustments(scored, PROFILE) == []


def test_reapply_adjustments_applies_new_boost_to_already_scored_job():
    sf_job = {**POSTING, "location": "San Francisco, CA", "fit_score": 85, "fit_rationale": "Strong match"}
    updated = reapply_adjustments([sf_job], LOCATION_PROFILE)
    assert len(updated) == 1
    assert updated[0]["fit_score"] == 93
    assert "Priority location boost" in updated[0]["fit_rationale"]


def test_reapply_adjustments_removes_boost_when_profile_reverts_to_unconfigured():
    sf_job = {
        **POSTING,
        "location": "San Francisco, CA",
        "fit_score": 93,
        "fit_rationale": "Strong match (Priority location boost: +8)",
    }
    updated = reapply_adjustments([sf_job], PROFILE)
    assert len(updated) == 1
    assert updated[0]["fit_score"] == 85
    assert updated[0]["fit_rationale"] == "Strong match"


def test_reapply_adjustments_migrates_legacy_bay_area_suffix():
    legacy_job = {
        **POSTING,
        "location": "San Francisco, CA",
        "fit_score": 93,
        "fit_rationale": "Strong match (Bay Area location boost: +8)",
    }
    updated = reapply_adjustments([legacy_job], LOCATION_PROFILE)
    assert len(updated) == 1
    assert updated[0]["fit_score"] == 93
    assert updated[0]["fit_rationale"] == "Strong match (Priority location boost: +8)"


def test_reapply_adjustments_recalculates_after_boost_amount_changes():
    sf_job = {
        **POSTING,
        "location": "San Francisco, CA",
        "fit_score": 93,
        "fit_rationale": "Strong match (Priority location boost: +8)",
    }
    bigger_boost_profile = {**LOCATION_PROFILE, "locations": {**LOCATION_PROFILE["locations"], "priority_location_boost": 15}}
    updated = reapply_adjustments([sf_job], bigger_boost_profile)
    assert len(updated) == 1
    assert updated[0]["fit_score"] == 100
    assert "Priority location boost: +15" in updated[0]["fit_rationale"]


def test_reapply_adjustments_skips_jobs_without_a_score():
    unscored = [{**POSTING, "fit_score": None, "fit_rationale": None}]
    assert reapply_adjustments(unscored, LOCATION_PROFILE) == []
