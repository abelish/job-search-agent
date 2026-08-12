"""
Tests for agents/aggregator — HTML utilities, ID generation, email parsers,
and fetch functions (HTTP calls are mocked via unittest.mock).
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from agents.aggregator import (
    _html_to_text,
    _make_id,
    _now,
    _split_gmail_job_text,
    _parse_linkedin_email,
    _parse_indeed_email,
    _indeed_job_key,
    fetch_greenhouse,
    fetch_lever,
    fetch_ashby,
)


# ---------------------------------------------------------------------------
# _html_to_text
# ---------------------------------------------------------------------------

def test_html_to_text_basic():
    result = _html_to_text("<p>Hello <b>world</b></p>")
    assert "Hello" in result
    assert "world" in result


def test_html_to_text_strips_tags():
    result = _html_to_text("<div><p>Clean text</p></div>")
    assert "<" not in result
    assert "Clean text" in result


def test_html_to_text_empty_string():
    assert _html_to_text("") == ""


def test_html_to_text_none_like_empty():
    assert _html_to_text("") == ""


def test_html_to_text_nested():
    result = _html_to_text("<ul><li>Item 1</li><li>Item 2</li></ul>")
    assert "Item 1" in result
    assert "Item 2" in result


# ---------------------------------------------------------------------------
# _make_id
# ---------------------------------------------------------------------------

def test_make_id_is_stable():
    a = _make_id("greenhouse", "https://example.com/job/1")
    b = _make_id("greenhouse", "https://example.com/job/1")
    assert a == b


def test_make_id_differs_by_source():
    a = _make_id("greenhouse", "https://example.com/job/1")
    b = _make_id("lever", "https://example.com/job/1")
    assert a != b


def test_make_id_differs_by_url():
    a = _make_id("greenhouse", "https://example.com/job/1")
    b = _make_id("greenhouse", "https://example.com/job/2")
    assert a != b


def test_make_id_length():
    assert len(_make_id("greenhouse", "https://example.com/job/1")) == 12


def test_make_id_is_hex():
    result = _make_id("lever", "https://example.com/job/99")
    int(result, 16)  # raises if not valid hex


# ---------------------------------------------------------------------------
# _split_gmail_job_text
# ---------------------------------------------------------------------------

def test_split_gmail_full_format():
    t, c, l = _split_gmail_job_text("Director of Engineering      Acme Corp • San Francisco, CA")
    assert t == "Director of Engineering"
    assert c == "Acme Corp"
    assert l == "San Francisco, CA"


def test_split_gmail_replacement_char_bullet():
    # U+FFFD appears when the bullet is mis-decoded by some email clients.
    t, c, l = _split_gmail_job_text("VP Engineering      Acme � New York, NY")
    assert t == "VP Engineering"
    assert c == "Acme"
    assert l == "New York, NY"


def test_split_gmail_with_salary_discarded():
    t, c, l = _split_gmail_job_text(
        "Director, Software Engineering      Walmart � Sunnyvale, CA      $208K-$416K / year"
    )
    assert t == "Director, Software Engineering"
    assert c == "Walmart"
    assert l == "Sunnyvale, CA"


def test_split_gmail_no_separator_title_only():
    t, c, l = _split_gmail_job_text("VP Engineering at Acme")
    assert t == "VP Engineering at Acme"
    assert c == ""
    assert l == ""


def test_split_gmail_company_no_location():
    t, c, l = _split_gmail_job_text("Engineering Manager      SomeCompany")
    assert t == "Engineering Manager"
    assert c == "SomeCompany"
    assert l == ""


def test_split_gmail_empty_string():
    t, c, l = _split_gmail_job_text("")
    assert t == ""
    assert c == ""
    assert l == ""


# ---------------------------------------------------------------------------
# _parse_linkedin_email
# The current parser matches bold anchors (style="font-weight:600") for titles
# and <p>Company &middot; Location</p> for company/location pairs.
# ---------------------------------------------------------------------------

FETCHED = "2026-06-24T00:00:00+00:00"

# Minimal HTML that matches the current LinkedIn email format.
def _li_anchor(job_id, title, extra_style=""):
    return (
        f'<a href="https://www.linkedin.com/jobs/view/{job_id}" '
        f'style="color:#000000;font-weight:600;{extra_style}">{title}</a>'
    )

def _li_company_p(company, location):
    return f'<p>{company} &middot; {location}</p>'


def test_parse_linkedin_email_extracts_job():
    html = _li_anchor("1234567890", "VP Engineering at Acme")
    result = _parse_linkedin_email(html, FETCHED)
    assert len(result) == 1
    assert result[0]["url"] == "https://www.linkedin.com/jobs/view/1234567890"
    assert result[0]["source"] == "linkedin_email"
    assert result[0]["title"] == "VP Engineering at Acme"


def test_parse_linkedin_email_parses_company_and_location():
    html = (
        _li_anchor("999", "Engineering Manager") + "\n" +
        _li_company_p("Acme Corp", "San Francisco, CA (Remote)")
    )
    result = _parse_linkedin_email(html, FETCHED)
    assert len(result) == 1
    assert result[0]["title"] == "Engineering Manager"
    assert result[0]["company"] == "Acme Corp"
    assert result[0]["location"] == "San Francisco, CA (Remote)"


def test_parse_linkedin_email_skips_non_bold_anchors():
    # Anchor without font-weight:600 should not be parsed as a job title.
    html = '<a href="https://www.linkedin.com/jobs/view/888">Some Job</a>'
    result = _parse_linkedin_email(html, FETCHED)
    assert result == []


def test_parse_linkedin_email_skips_empty_text():
    html = _li_anchor("888", "")
    result = _parse_linkedin_email(html, FETCHED)
    assert result == []


def test_parse_linkedin_email_skips_whitespace_only_text():
    html = _li_anchor("888", "   ")
    result = _parse_linkedin_email(html, FETCHED)
    assert result == []


def test_parse_linkedin_email_deduplicates():
    html = (
        _li_anchor("999", "Job A") + "\n" +
        _li_anchor("999", "Job A again")
    )
    result = _parse_linkedin_email(html, FETCHED)
    assert len(result) == 1


def test_parse_linkedin_email_multiple_jobs():
    html = (
        _li_anchor("111", "Job One") + "\n" +
        _li_anchor("222", "Job Two")
    )
    result = _parse_linkedin_email(html, FETCHED)
    assert len(result) == 2


def test_parse_linkedin_email_no_jobs():
    result = _parse_linkedin_email("<p>No jobs here</p>", FETCHED)
    assert result == []


def test_parse_linkedin_email_comm_url_variant():
    html = (
        '<a href="https://www.linkedin.com/comm/jobs/view/555" '
        'style="font-weight:600">Engineering Manager</a>'
    )
    result = _parse_linkedin_email(html, FETCHED)
    assert len(result) == 1
    assert "555" in result[0]["url"]


def test_parse_linkedin_email_fetched_date_set():
    html = _li_anchor("123", "Job")
    result = _parse_linkedin_email(html, FETCHED)
    assert result[0]["fetched_date"] == FETCHED


# ---------------------------------------------------------------------------
# _parse_indeed_email
# ---------------------------------------------------------------------------

def test_parse_indeed_email_viewjob_url():
    html = '<a href="https://www.indeed.com/viewjob?jk=abc123xyz">Director of Eng</a>'
    result = _parse_indeed_email(html, FETCHED)
    assert len(result) == 1
    assert "abc123xyz" in result[0]["url"]
    assert result[0]["source"] == "indeed_email"


def test_parse_indeed_email_parses_company_and_location():
    html = (
        '<a href="https://www.indeed.com/viewjob?jk=abc123">'
        'Director of Engineering      Acme � Austin, TX</a>'
    )
    result = _parse_indeed_email(html, FETCHED)
    assert len(result) == 1
    assert result[0]["title"] == "Director of Engineering"
    assert result[0]["company"] == "Acme"
    assert result[0]["location"] == "Austin, TX"


def test_parse_indeed_email_skips_empty_text():
    html = '<a href="https://www.indeed.com/viewjob?jk=empty999"></a>'
    result = _parse_indeed_email(html, FETCHED)
    assert result == []


def test_parse_indeed_email_rc_clk_url():
    html = '<a href="https://www.indeed.com/rc/clk?jk=def456uvw">Engineering Lead</a>'
    result = _parse_indeed_email(html, FETCHED)
    assert len(result) == 1
    assert "def456uvw" in result[0]["url"]


def test_parse_indeed_email_deduplicates():
    html = """
        <a href="https://www.indeed.com/viewjob?jk=aaa111">Job</a>
        <a href="https://www.indeed.com/viewjob?jk=aaa111">Job again</a>
    """
    result = _parse_indeed_email(html, FETCHED)
    assert len(result) == 1


def test_parse_indeed_email_no_jobs():
    result = _parse_indeed_email("<p>Nothing</p>", FETCHED)
    assert result == []


def test_parse_indeed_email_rc_clk_dl_url():
    # Current Indeed alert template: rc/clk/dl (with the extra /dl segment),
    # company and location as separate sibling <p> tags rather than bundled
    # into the anchor text.
    html = (
        '<h2><a href="https://www.indeed.com/rc/clk/dl?jk=abc123def4567890&from=ja">'
        "Director of Engineering</a></h2>"
        "<p>Acme Corp</p>"
        "<p>Austin, TX</p>"
    )
    result = _parse_indeed_email(html, FETCHED)
    assert len(result) == 1
    assert result[0]["title"] == "Director of Engineering"
    assert result[0]["company"] == "Acme Corp"
    assert result[0]["location"] == "Austin, TX"
    assert "abc123def4567890" in result[0]["url"]


def test_parse_indeed_email_captures_salary_into_description():
    html = (
        '<h2><a href="https://www.indeed.com/rc/clk/dl?jk=abc123def4567890">Eng Lead</a></h2>'
        "<p>Acme Corp</p>"
        "<p>Austin, TX</p>"
        "<p>$150,000 - $200,000 a year</p>"
        "<p>Just posted</p>"
    )
    result = _parse_indeed_email(html, FETCHED)
    assert result[0]["company"] == "Acme Corp"
    assert result[0]["location"] == "Austin, TX"
    assert result[0]["description"] == "Compensation: $150,000 - $200,000 a year"


def test_parse_indeed_email_no_salary_leaves_description_blank():
    html = (
        '<h2><a href="https://www.indeed.com/rc/clk/dl?jk=abc123def4567890">Eng Lead</a></h2>'
        "<p>Acme Corp</p>"
        "<p>Austin, TX</p>"
        "<p>Just posted</p>"
    )
    result = _parse_indeed_email(html, FETCHED)
    assert result[0]["description"] == ""


def test_parse_indeed_email_recommended_jobs_pagead_format():
    # "Recommended jobs" digest links through a sponsored pagead/clk redirect
    # instead of a direct ?jk= link; the job key is the final hyphen-separated
    # segment of jrtk=.
    html = (
        '<h2><a href="https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob'
        '&jrtk=5-cmh1-1-1jvpi1ejglia5807-abcdef0123456789&rm=2">'
        "Principal Engineer</a></h2>"
        "<p>Foo Inc</p>"
        "<p>Remote</p>"
    )
    result = _parse_indeed_email(html, FETCHED)
    assert len(result) == 1
    assert result[0]["title"] == "Principal Engineer"
    assert result[0]["company"] == "Foo Inc"
    assert result[0]["location"] == "Remote"
    assert result[0]["url"] == "https://www.indeed.com/viewjob?jk=abcdef0123456789"


def test_parse_indeed_email_multiple_cards_dont_bleed_together():
    html = (
        '<h2><a href="https://www.indeed.com/rc/clk/dl?jk=1111111111111111">Job One</a></h2>'
        "<p>Company One</p>"
        "<p>City One</p>"
        '<h2><a href="https://www.indeed.com/rc/clk/dl?jk=2222222222222222">Job Two</a></h2>'
        "<p>Company Two</p>"
        "<p>City Two</p>"
    )
    result = _parse_indeed_email(html, FETCHED)
    assert len(result) == 2
    assert result[0]["company"] == "Company One"
    assert result[0]["location"] == "City One"
    assert result[1]["company"] == "Company Two"
    assert result[1]["location"] == "City Two"


# ---------------------------------------------------------------------------
# _indeed_job_key
# ---------------------------------------------------------------------------

def test_indeed_job_key_viewjob():
    assert _indeed_job_key("https://www.indeed.com/viewjob?jk=abc123xyz") == "abc123xyz"


def test_indeed_job_key_rc_clk_dl():
    href = "https://www.indeed.com/rc/clk/dl?jk=f8b00095fea80694&from=ja"
    assert _indeed_job_key(href) == "f8b00095fea80694"


def test_indeed_job_key_pagead_jrtk():
    href = (
        "https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob"
        "&jrtk=5-cmh1-1-1jvpi1ejglia5807-39795eaee9ed8f65&rm=2"
    )
    assert _indeed_job_key(href) == "39795eaee9ed8f65"


def test_indeed_job_key_jrtk_non_hex_segment_ignored():
    # If the trailing jrtk segment isn't a 16-char hex job key, don't guess.
    href = "https://www.indeed.com/pagead/clk/dl?jrtk=5-cmh1-1-notahexkey"
    assert _indeed_job_key(href) is None


def test_indeed_job_key_non_job_link():
    assert _indeed_job_key("https://www.indeed.com/legal?hl=en") is None


# ---------------------------------------------------------------------------
# fetch_greenhouse
# ---------------------------------------------------------------------------

def _mock_response(json_data, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.raise_for_status = MagicMock()
    return m


def test_fetch_greenhouse_normalizes_posting():
    board_resp = _mock_response({"name": "Acme Corp"})
    jobs_resp = _mock_response({"jobs": [{
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "title": "VP Engineering",
        "location": {"name": "San Francisco, CA"},
        "updated_at": "2026-06-01T00:00:00Z",
        "content": "<p>Great role for a leader.</p>",
    }]})
    with patch("agents.aggregator.requests.get", side_effect=[board_resp, jobs_resp]):
        result = fetch_greenhouse("acme")
    assert len(result) == 1
    assert result[0]["title"] == "VP Engineering"
    assert result[0]["source"] == "greenhouse"
    assert result[0]["company"] == "Acme Corp"
    assert result[0]["location"] == "San Francisco, CA"
    assert "Great role" in result[0]["description"]
    assert len(result[0]["id"]) == 12


def test_fetch_greenhouse_skips_jobs_without_url():
    board_resp = _mock_response({"name": "Acme"})
    jobs_resp = _mock_response({"jobs": [
        {"absolute_url": "", "title": "No URL Job", "location": {"name": ""}, "updated_at": ""},
        {"absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "title": "Good Job",
         "location": {"name": "Remote"}, "updated_at": "2026-06-01T00:00:00Z", "content": ""},
    ]})
    with patch("agents.aggregator.requests.get", side_effect=[board_resp, jobs_resp]):
        result = fetch_greenhouse("acme")
    assert len(result) == 1
    assert result[0]["title"] == "Good Job"


# ---------------------------------------------------------------------------
# fetch_lever
# ---------------------------------------------------------------------------

def test_fetch_lever_normalizes_posting():
    resp = _mock_response([{
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "text": "Director of Engineering",
        "categories": {"location": "San Francisco, CA"},
        "descriptionPlain": "Lead our engineering org.",
        "createdAt": 1748736000000,
    }])
    with patch("agents.aggregator.requests.get", return_value=resp):
        result = fetch_lever("acme")
    assert len(result) == 1
    assert result[0]["title"] == "Director of Engineering"
    assert result[0]["source"] == "lever"
    assert result[0]["location"] == "San Francisco, CA"
    assert result[0]["description"] == "Lead our engineering org."


def test_fetch_lever_skips_jobs_without_url():
    resp = _mock_response([
        {"hostedUrl": "", "text": "Bad Job", "categories": {}, "createdAt": 0},
        {"hostedUrl": "https://jobs.lever.co/acme/xyz", "text": "Good Job",
         "categories": {"location": "Remote"}, "descriptionPlain": "Great.", "createdAt": 1748736000000},
    ])
    with patch("agents.aggregator.requests.get", return_value=resp):
        result = fetch_lever("acme")
    assert len(result) == 1
    assert result[0]["title"] == "Good Job"


# ---------------------------------------------------------------------------
# fetch_ashby
# ---------------------------------------------------------------------------

def test_fetch_ashby_normalizes_posting_v1():
    resp = _mock_response({
        "apiVersion": 1,
        "jobs": [{
            "jobUrl": "https://jobs.ashbyhq.com/ramp/abc-123",
            "title": "Head of Engineering",
            "location": "New York, NY",
            "descriptionPlain": "Join our team.",
            "publishedAt": "2026-06-01T00:00:00.000+00:00",
            "isListed": True,
        }],
    })
    with patch("agents.aggregator.requests.get", return_value=resp):
        result = fetch_ashby("ramp")
    assert len(result) == 1
    assert result[0]["title"] == "Head of Engineering"
    assert result[0]["source"] == "ashby"
    assert result[0]["company"] == "Ramp"
    assert result[0]["location"] == "New York, NY"
    assert result[0]["posted_date"] == "2026-06-01"
    assert "Join our team" in result[0]["description"]


def test_fetch_ashby_normalizes_posting_legacy():
    resp = _mock_response({
        "organization": {"name": "Ramp"},
        "jobPostings": [{
            "jobUrl": "https://jobs.ashbyhq.com/ramp/abc-123",
            "title": "Head of Engineering",
            "locationName": "New York, NY",
            "descriptionHtml": "<p>Join our team.</p>",
            "publishedDate": "2026-06-01",
        }],
    })
    with patch("agents.aggregator.requests.get", return_value=resp):
        result = fetch_ashby("ramp")
    assert len(result) == 1
    assert result[0]["company"] == "Ramp"
    assert result[0]["posted_date"] == "2026-06-01"
    assert "Join our team" in result[0]["description"]


def test_fetch_ashby_skips_unlisted_jobs():
    resp = _mock_response({
        "apiVersion": 1,
        "jobs": [
            {"jobUrl": "https://jobs.ashbyhq.com/ramp/hidden", "title": "Unlisted",
             "location": "Remote", "isListed": False, "publishedAt": "2026-06-01T00:00:00Z"},
            {"jobUrl": "https://jobs.ashbyhq.com/ramp/xyz", "title": "Good Job",
             "location": "Remote", "isListed": True, "publishedAt": "2026-06-01T00:00:00Z"},
        ],
    })
    with patch("agents.aggregator.requests.get", return_value=resp):
        result = fetch_ashby("ramp")
    assert len(result) == 1
    assert result[0]["title"] == "Good Job"


def test_fetch_ashby_skips_jobs_without_url():
    resp = _mock_response({
        "apiVersion": 1,
        "jobs": [
            {"jobUrl": "", "title": "No URL", "location": "", "isListed": True, "publishedAt": ""},
            {"jobUrl": "https://jobs.ashbyhq.com/ramp/xyz", "title": "Good Job",
             "location": "Remote", "isListed": True, "publishedAt": "2026-06-01T00:00:00Z"},
        ],
    })
    with patch("agents.aggregator.requests.get", return_value=resp):
        result = fetch_ashby("ramp")
    assert len(result) == 1


def test_fetch_ashby_falls_back_to_board_name_for_company():
    resp = _mock_response({
        "apiVersion": 1,
        "jobs": [{
            "jobUrl": "https://jobs.ashbyhq.com/open-ai/abc",
            "title": "Engineer",
            "location": "Remote",
            "isListed": True,
            "publishedAt": "2026-06-01T00:00:00Z",
        }],
    })
    with patch("agents.aggregator.requests.get", return_value=resp):
        result = fetch_ashby("open-ai")
    assert result[0]["company"] == "Open Ai"
