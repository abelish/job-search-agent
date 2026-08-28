"""
Fit scorer agent.

Scores a normalized job posting against data/profile/profile.json.
Returns a score (0-100) and a short rationale.

Flow:
1. passes_hard_filters — cheap, deterministic pre-filter (no Claude call)
2. score_posting       — Claude call for score + rationale
3. score_all           — runs both steps over a list, returns those above threshold
"""

import json
import re
import sys

from agents.claude_client import DEFAULT_MODEL, complete
from tracker import log_activity


def _extract_json(text: str) -> str:
    """Strip markdown code fences and return the inner JSON string."""
    text = text.strip()
    # ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        return match.group(1).strip()
    return text

SCORE_PROMPT = """You are scoring how well a job posting fits a candidate profile.

Candidate profile:
{profile}

Job posting:
{posting}

Return only JSON in this exact shape, nothing else:
{{"score": <integer 0-100>, "rationale": "<one paragraph, no hyphens or semicolons>"}}

Score meaning:
90-100  Excellent match on role, seniority, domain, and location.
70-89   Good match with minor gaps worth discussing.
50-69   Partial match. Significant stretch in at least one dimension.
0-49    Poor fit. Missing critical requirements or hard constraints violated.

Compensation note: Before concluding that compensation is unlisted, read the full job description
carefully for any mention of salary range, base pay, total compensation, equity, OTE, or annual
pay. Compensation figures often appear at the end of the description after the requirements
section. If a range or figure is present anywhere in the description, cite it in your rationale
and factor it against the candidate's stated minimum compensation.

Industry and mission fit note: If the candidate profile above names industries to favor or avoid,
judge the posting's company against them using the job description and your general knowledge of
the company, not keyword matching. A company whose core, primary business falls in an avoided
industry should score no higher than 35 even if it otherwise matches well, unless the specific
role is clearly walled off from that core business (e.g. a renewable energy team inside an
otherwise diversified company). Don't penalize a company for an incidental client, contract, or
subsidiary in an avoided industry when its core business is elsewhere. A company whose core
business falls in a favored industry should score 10 to 15 points higher than an otherwise
identical role at a neutral company. If the profile names no industries either way, ignore this
note entirely.
"""

# Any location bias (e.g. "boost roles near me") is applied as a deterministic,
# profile-configured adjustment after Claude scores — see _location_adjustment_config
# and score_posting — rather than baked into the prompt above, so it's visible and
# tunable per candidate instead of hidden in the prompt template.

# Titles at director level or above — roles below this threshold are the ones
# eligible for the profile's non_priority_manager_penalty, since manager-level
# comp ranges vary with location much more than director+ ranges do.
_DIRECTOR_PLUS_TERMS = {
    "director", "vp", "vice president", "head of", "cto", "chief",
}


def _is_director_plus(title: str) -> bool:
    t = title.lower()
    return any(term in t for term in _DIRECTOR_PLUS_TERMS)


def _location_adjustment_config(profile: dict) -> tuple[list[str], int, int]:
    """
    Read the optional, opt-in location scoring adjustment from profile.json:

        "locations": {
            "priority_location_terms": ["San Francisco", "Bay Area", ...],
            "priority_location_boost": 8,
            "non_priority_manager_penalty": 10
        }

    priority_location_terms is a list of case-insensitive substrings matched
    against the posting's location. A match adds priority_location_boost
    points. No match, on a role below director level, subtracts
    non_priority_manager_penalty points (director+ roles are assumed to clear
    the comp floor regardless of location, so they're exempt).

    All three default to off (empty terms / 0 points) so a profile that
    doesn't configure this gets no location-based scoring bias at all.
    """
    locs = profile.get("locations", {})
    terms = [t.lower() for t in locs.get("priority_location_terms", [])]
    boost = locs.get("priority_location_boost", 0)
    penalty = locs.get("non_priority_manager_penalty", 0)
    return terms, boost, penalty


def hard_filter_reason(posting: dict, profile: dict) -> str | None:
    """
    Deterministic pre-filter against hard profile constraints.
    Returns a human-readable reason on the first hard fail, or None if the
    posting passes. Saves Claude calls for clear misses.

    Checks:
    - exclude_companies (case-insensitive company name match)
    - exclude_title_keywords (ANY match in title blocks the posting)
    - must_have_keywords (ALL must appear in title + description)
    - location (remote_ok, city_allowlist_international, willing_to_relocate)

    Note: comp_floor_usd is not checked here because compensation data is
    rarely structured in public API postings; it is left to the scorer prompt.
    """
    company = posting.get("company", "").lower()
    excluded = {c.lower() for c in profile.get("exclude_companies", [])}
    if company and company in excluded:
        return f"Company \"{posting.get('company', '')}\" is on your excluded companies list."

    title_lower = posting.get("title", "").lower()
    for kw in profile.get("exclude_title_keywords", []):
        if kw.lower() in title_lower:
            return f"Title contains the excluded keyword \"{kw}\"."

    must_have = profile.get("must_have_keywords", [])
    if must_have:
        searchable = (posting.get("title", "") + " " + posting.get("description", "")).lower()
        missing = [kw for kw in must_have if kw.lower() not in searchable]
        if missing:
            return f"Missing required keyword(s): {', '.join(missing)}."

    # Title keyword filter: at least ONE must appear in the job title (OR logic).
    # Use this to restrict to specific role levels or functions without touching description.
    must_have_title = profile.get("must_have_title_keywords", [])
    if must_have_title:
        title = posting.get("title", "").lower()
        if not any(kw.lower() in title for kw in must_have_title):
            return "Title doesn't match any of your required title keywords."

    location = posting.get("location", "").lower()
    if location:
        locs = profile.get("locations", {})
        is_remote = "remote" in location
        if is_remote and not locs.get("remote_ok", True):
            return "Posting is remote, but your profile has remote roles turned off."
        if not is_remote and not locs.get("willing_to_relocate", False):
            preferred = [c.lower() for c in locs.get("city_allowlist_international", [])]
            if preferred and not any(c in location for c in preferred):
                return f"Location \"{posting.get('location', '')}\" isn't in your city allowlist, and you're not marked as willing to relocate."
        # German sources always enforce city_allowlist_international regardless of willing_to_relocate,
        # since the user may be open to relocating globally but still only want Berlin in Germany.
        if not is_remote and posting.get("source") in {"arbeitsagentur", "smartrecruiters"}:
            preferred = [c.lower() for c in locs.get("city_allowlist_international", [])]
            if preferred and not any(c in location for c in preferred):
                return f"Location \"{posting.get('location', '')}\" isn't in your city allowlist for German-sourced postings."

    # English workplace: reject postings written primarily in German.
    # Uses a word-count heuristic — borderline cases pass through to Claude for scoring.
    if profile.get("require_english_workplace"):
        text = (posting.get("title", "") + " " + posting.get("description", "")[:1000]).lower()
        german_markers = [
            "wir suchen", "ihre aufgaben", "ihr profil", "was wir bieten",
            "du bringst", "deine aufgaben", "bewerbung", "kenntnisse",
            " und ", " oder ", " mit ", " für ", " bei ", " ist ",
        ]
        if sum(1 for m in german_markers if m in text) >= 4:
            return "Posting appears to be written primarily in German."
        # For German-source postings in English: reject if German language is explicitly required.
        if posting.get("source") in {"arbeitsagentur", "smartrecruiters"}:
            full_text = (posting.get("title", "") + " " + posting.get("description", "")).lower()
            german_required_markers = [
                "german required", "german language required", "german fluency",
                "german proficiency", "must speak german", "german speaking required",
                "deutsch erforderlich", "deutschkenntnisse erforderlich", "fließend deutsch",
            ]
            if any(m in full_text for m in german_required_markers):
                return "Posting explicitly requires German language proficiency."

    return None


def passes_hard_filters(posting: dict, profile: dict) -> bool:
    """Convenience boolean wrapper around hard_filter_reason."""
    return hard_filter_reason(posting, profile) is None


def _fmt_profile(profile: dict) -> str:
    lines = []
    if profile.get("name"):
        lines.append(f"Name: {profile['name']}")
    if profile.get("current_title"):
        lines.append(f"Current title: {profile['current_title']}")
    if profile.get("target_titles"):
        lines.append(f"Target titles: {', '.join(profile['target_titles'])}")
    if profile.get("years_experience"):
        lines.append(f"Years of experience: {profile['years_experience']}")
    if profile.get("core_skills"):
        lines.append(f"Core skills: {', '.join(profile['core_skills'])}")
    if profile.get("domains"):
        lines.append(f"Domains: {', '.join(profile['domains'])}")
    if profile.get("comp_floor_usd"):
        lines.append(f"Minimum compensation: ${profile['comp_floor_usd']:,} USD")
    locs = profile.get("locations", {})
    if locs:
        parts = []
        if locs.get("remote_ok"):
            parts.append("remote OK")
        if locs.get("city_allowlist_international"):
            parts.append("city allowlist (hard requirement for international roles): " + ", ".join(locs["city_allowlist_international"]))
        if locs.get("preferred_regions_us"):
            parts.append("preferred regions (soft preference, not a hard requirement): " + ", ".join(locs["preferred_regions_us"]))
        if locs.get("willing_to_relocate"):
            parts.append("open to roles anywhere in the US")
        if parts:
            lines.append("Location: " + "; ".join(parts))
    if profile.get("must_have_title_keywords"):
        lines.append(f"Role type filter (title must include one of): {', '.join(profile['must_have_title_keywords'])}")
    industry_prefs = profile.get("industry_preferences", {})
    if industry_prefs.get("favor"):
        lines.append(f"Favor companies whose core business relates to: {', '.join(industry_prefs['favor'])}")
    if industry_prefs.get("avoid"):
        lines.append(f"Avoid companies whose core business relates to: {', '.join(industry_prefs['avoid'])}")
    if profile.get("exclude_companies"):
        lines.append(f"Not interested in: {', '.join(profile['exclude_companies'])}")
    if profile.get("exclude_title_keywords"):
        lines.append(f"Exclude roles with these words in title: {', '.join(profile['exclude_title_keywords'])}")
    if profile.get("require_english_workplace"):
        lines.append(
            "Requirement: English must be the primary working language. "
            "For roles based in Berlin or Germany posted in English, assume English is the working language "
            "unless the listing explicitly states German proficiency is required."
        )
    if profile.get("require_visa_sponsorship_for_international_roles"):
        lines.append("Requirement: for roles based outside the United States, company must sponsor work visas or offer relocation support for non-EU candidates — this requirement does not apply to US-based or US-remote roles")
    if profile.get("notes"):
        lines.append(f"Additional notes: {profile['notes']}")
    return "\n".join(lines)


def _fmt_posting(posting: dict) -> str:
    lines = [
        f"Title: {posting.get('title', '')}",
        f"Company: {posting.get('company', '')}",
        f"Location: {posting.get('location', '')}",
        f"URL: {posting.get('url', '')}",
        "",
        "Description:",
        posting.get("description", "")[:15000],
    ]
    return "\n".join(lines)


def score_posting(posting: dict, profile: dict) -> dict:
    """
    Call Claude to score a single posting. Returns the posting dict updated
    with fit_score (int) and fit_rationale (str).
    Token usage is returned in _input_tokens / _output_tokens / _model for
    aggregation by score_all.
    """
    prompt = SCORE_PROMPT.format(
        profile=_fmt_profile(profile),
        posting=_fmt_posting(posting),
    )
    result = complete(prompt)
    raw = result["text"]

    # Retry once on empty response (transient API issue)
    if not raw.strip():
        print(f"  Empty response for {posting.get('id')}, retrying...", file=sys.stderr)
        result = complete(prompt)
        raw = result["text"]

    try:
        parsed = json.loads(_extract_json(raw))
        score = int(parsed["score"])
        rationale = str(parsed.get("rationale", ""))
    except Exception as e:
        print(f"  Score parse error for {posting.get('id')}: {e}", file=sys.stderr)
        print(f"  Raw response: {raw!r}", file=sys.stderr)
        score = 0
        rationale = f"Parse error: {raw[:200]}"

    score, rationale = _apply_location_adjustment(score, rationale, posting, profile)

    return {
        **posting,
        "fit_score": score,
        "fit_rationale": rationale,
        "_input_tokens": result["input_tokens"],
        "_output_tokens": result["output_tokens"],
        "_model": result["model"],
    }


def _apply_location_adjustment(score: int, rationale: str, posting: dict, profile: dict) -> tuple[int, str]:
    """Apply the profile's configured location boost/penalty (see _location_adjustment_config)."""
    terms, boost, penalty = _location_adjustment_config(profile)
    if not terms:
        return score, rationale

    location_lower = posting.get("location", "").lower()
    is_priority_location = any(term in location_lower for term in terms)

    if is_priority_location and boost:
        score = min(100, score + boost)
        rationale = rationale + f" (Priority location boost: +{boost})"
    elif not is_priority_location and penalty and not _is_director_plus(posting.get("title", "")):
        score = max(0, score - penalty)
        rationale = rationale + f" (Non-priority manager-level penalty: -{penalty})"

    return score, rationale


def _strip_adjustment(score: int, rationale: str) -> tuple[int, str]:
    """
    Reverse any previously applied score adjustment suffix, returning
    (raw_claude_score, clean_rationale) so adjustments can be re-applied fresh.
    """
    m = re.search(r' \(Priority location boost: \+(\d+)\)$', rationale)
    if m:
        return score - int(m.group(1)), rationale[:m.start()]
    m = re.search(r' \(Non-priority manager-level penalty: -(\d+)\)$', rationale)
    if m:
        return score + int(m.group(1)), rationale[:m.start()]
    # Legacy suffixes from before the location adjustment became profile-configurable.
    m = re.search(r' \(Bay Area location boost: \+(\d+)\)$', rationale)
    if m:
        return score - int(m.group(1)), rationale[:m.start()]
    m = re.search(r' \(Non-Bay-Area manager-level penalty: -(\d+)\)$', rationale)
    if m:
        return score + int(m.group(1)), rationale[:m.start()]
    return score, rationale


def reapply_adjustments(jobs: list[dict], profile: dict) -> list[dict]:
    """
    Re-apply the location/seniority adjustment to already-scored jobs without
    calling Claude. Strips any existing adjustment suffix, recovers the raw
    Claude score, then applies the profile's current boost/penalty config.

    Returns only jobs whose score or rationale actually changed.
    """
    updated = []
    for job in jobs:
        score = job.get("fit_score")
        rationale = job.get("fit_rationale") or ""
        if score is None or not rationale:
            continue

        raw_score, clean_rationale = _strip_adjustment(score, rationale)
        new_score, new_rationale = _apply_location_adjustment(raw_score, clean_rationale, job, profile)

        if new_score != score or new_rationale != rationale:
            updated.append({**job, "fit_score": new_score, "fit_rationale": new_rationale})

    return updated


def score_all(postings: list[dict], profile: dict, threshold: int = 70, on_progress: callable = None, should_stop: callable = None) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Run hard_filter_reason then score_posting on each posting.
    Returns (above_threshold, below_threshold, filtered), each sorted by score
    descending (filtered postings have no score, so they keep input order).
    above/below carry the real fit_score and fit_rationale from Claude.
    filtered postings never reach Claude — fit_score is None and fit_rationale
    explains which hard filter rejected them, so the caller can surface why a
    job was skipped instead of leaving it stuck looking unscored.
    Logs a score_run activity entry with total token usage.
    """
    above: list[dict] = []
    below: list[dict] = []
    filtered: list[dict] = []
    total_input = 0
    total_output = 0
    model = DEFAULT_MODEL

    for posting in postings:
        if should_stop and should_stop():
            break
        reason = hard_filter_reason(posting, profile)
        if reason is not None:
            filtered.append({**posting, "fit_score": None, "fit_rationale": f"Filtered out: {reason}"})
            if on_progress:
                on_progress()
            continue
        scored = score_posting(posting, profile)
        total_input += scored.pop("_input_tokens", 0)
        total_output += scored.pop("_output_tokens", 0)
        model = scored.pop("_model", model)
        if scored["fit_score"] >= threshold:
            above.append(scored)
        else:
            below.append(scored)
        if on_progress:
            on_progress()

    above.sort(key=lambda j: j.get("fit_score", 0), reverse=True)
    below.sort(key=lambda j: j.get("fit_score", 0), reverse=True)

    log_activity("score_run", detail={
        "total_postings": len(postings),
        "skipped_hard_filter": len(filtered),
        "above_threshold": len(above),
        "threshold": threshold,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "model": model,
    })

    return above, below, filtered
