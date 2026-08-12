"""
Tests for tracker — schema, upsert, status updates, activity log, stats, token summary.
Each test gets a fresh in-memory database via the temp_db fixture.
"""

import json
import pytest

import tracker as tr


JOB = {
    "id": "test001",
    "source": "greenhouse",
    "title": "VP Engineering",
    "company": "Acme",
    "location": "San Francisco, CA",
    "url": "https://example.com/job/1",
    "description": "Lead our engineering org.",
    "posted_date": "2026-06-01",
    "fetched_date": "2026-06-24T00:00:00+00:00",
    "fit_score": 85,
    "fit_rationale": "Strong match on seniority and domain.",
    "status": "new",
    "resume_draft": None,
    "cover_letter_draft": None,
    "prep_brief": None,
}


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(tr, "DB_PATH", db)
    tr.init_db()


# ---------------------------------------------------------------------------
# upsert_job / get_job
# ---------------------------------------------------------------------------

def test_upsert_and_retrieve():
    tr.upsert_job(JOB)
    result = tr.get_job("test001")
    assert result is not None
    assert result["title"] == "VP Engineering"
    assert result["company"] == "Acme"
    assert result["fit_score"] == 85


def test_get_job_not_found_returns_none():
    assert tr.get_job("doesnotexist") is None


def test_upsert_updates_title_on_conflict():
    tr.upsert_job(JOB)
    tr.upsert_job({**JOB, "title": "Senior VP Engineering"})
    assert tr.get_job("test001")["title"] == "Senior VP Engineering"


def test_upsert_preserves_existing_score_when_none():
    tr.upsert_job(JOB)
    tr.upsert_job({**JOB, "fit_score": None, "fit_rationale": None})
    result = tr.get_job("test001")
    assert result["fit_score"] == 85
    assert result["fit_rationale"] == "Strong match on seniority and domain."


def test_upsert_preserves_existing_status_when_none():
    tr.upsert_job({**JOB, "status": "drafted"})
    tr.upsert_job({**JOB, "status": None})
    assert tr.get_job("test001")["status"] == "drafted"


def test_upsert_preserves_resume_draft_when_none():
    tr.upsert_job({**JOB, "resume_draft": "My tailored resume."})
    tr.upsert_job({**JOB, "resume_draft": None})
    assert tr.get_job("test001")["resume_draft"] == "My tailored resume."


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------

def test_list_jobs_returns_all():
    tr.upsert_job(JOB)
    tr.upsert_job({**JOB, "id": "test002", "title": "Director of Engineering"})
    assert len(tr.list_jobs()) == 2


def test_list_jobs_filtered_by_status():
    tr.upsert_job(JOB)
    tr.upsert_job({**JOB, "id": "test002", "status": "scored"})
    new_jobs = tr.list_jobs(status="new")
    assert len(new_jobs) == 1
    assert new_jobs[0]["id"] == "test001"


def test_list_jobs_empty():
    assert tr.list_jobs() == []


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------

def test_update_status_changes_status():
    tr.upsert_job(JOB)
    tr.update_status("test001", "scored")
    assert tr.get_job("test001")["status"] == "scored"


def test_update_status_all_valid_statuses():
    tr.upsert_job(JOB)
    for status in tr.VALID_STATUSES:
        tr.update_status("test001", status)
        assert tr.get_job("test001")["status"] == status


def test_update_status_invalid_raises():
    tr.upsert_job(JOB)
    with pytest.raises(ValueError):
        tr.update_status("test001", "pending")


# ---------------------------------------------------------------------------
# log_activity / list_activity
# ---------------------------------------------------------------------------

def test_log_and_list_activity():
    tr.log_activity("scan_run", detail={"count": 42, "sources": ["greenhouse"]})
    activity = tr.list_activity()
    assert len(activity) == 1
    assert activity[0]["event_type"] == "scan_run"
    assert activity[0]["detail"]["count"] == 42


def test_log_activity_with_job_id():
    tr.upsert_job(JOB)
    tr.log_activity("draft_generated", job_id="test001", detail={"fields": ["resume_draft"]})
    activity = tr.list_activity()
    assert activity[0]["job_id"] == "test001"


def test_list_activity_respects_limit():
    for i in range(10):
        tr.log_activity("scan_run", detail={"i": i})
    assert len(tr.list_activity(limit=5)) == 5


def test_list_activity_newest_first():
    tr.log_activity("scan_run", detail={"order": 1})
    tr.log_activity("scan_run", detail={"order": 2})
    activity = tr.list_activity()
    assert activity[0]["detail"]["order"] == 2


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

def test_get_stats_counts_by_status():
    tr.upsert_job(JOB)
    tr.upsert_job({**JOB, "id": "test002", "status": "scored"})
    tr.upsert_job({**JOB, "id": "test003", "status": "scored"})
    stats = tr.get_stats()
    assert stats["new"] == 1
    assert stats["scored"] == 2
    assert stats["drafted"] == 0


def test_get_stats_returns_all_statuses():
    tr.get_stats()  # should not raise even with empty DB
    stats = tr.get_stats()
    for status in tr.VALID_STATUSES:
        assert status in stats


# ---------------------------------------------------------------------------
# chat (resume / cover letter iteration)
# ---------------------------------------------------------------------------

def test_get_chat_empty_when_no_history():
    tr.upsert_job(JOB)
    assert tr.get_chat("test001", "resume") == []


def test_record_chat_turn_appends_history_and_updates_draft():
    tr.upsert_job({**JOB, "resume_draft": "Original resume."})
    tr.record_chat_turn("test001", "resume", "Make it punchier", "Made it punchier.", "Punchier resume.")
    history = tr.get_chat("test001", "resume")
    assert len(history) == 2
    assert history[0] == {"role": "user", "content": "Make it punchier", "created_at": history[0]["created_at"]}
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Made it punchier."
    assert tr.get_job("test001")["resume_draft"] == "Punchier resume."


def test_record_chat_turn_accumulates_across_calls():
    tr.upsert_job({**JOB, "resume_draft": "v1"})
    tr.record_chat_turn("test001", "resume", "feedback 1", "reply 1", "v2")
    tr.record_chat_turn("test001", "resume", "feedback 2", "reply 2", "v3")
    history = tr.get_chat("test001", "resume")
    assert len(history) == 4
    assert tr.get_job("test001")["resume_draft"] == "v3"


def test_record_chat_turn_sections_are_independent():
    tr.upsert_job({**JOB, "resume_draft": "resume v1", "cover_letter_draft": "cl v1"})
    tr.record_chat_turn("test001", "resume", "fb", "reply", "resume v2")
    assert tr.get_chat("test001", "cover_letter") == []
    assert tr.get_job("test001")["cover_letter_draft"] == "cl v1"


def test_clear_chat_resets_history_but_keeps_draft():
    tr.upsert_job({**JOB, "resume_draft": "v1"})
    tr.record_chat_turn("test001", "resume", "fb", "reply", "v2")
    tr.clear_chat("test001", "resume")
    assert tr.get_chat("test001", "resume") == []
    assert tr.get_job("test001")["resume_draft"] == "v2"


def test_chat_invalid_section_raises():
    tr.upsert_job(JOB)
    with pytest.raises(ValueError):
        tr.get_chat("test001", "bogus")
    with pytest.raises(ValueError):
        tr.record_chat_turn("test001", "bogus", "fb", "reply", "draft")
    with pytest.raises(ValueError):
        tr.clear_chat("test001", "bogus")


# ---------------------------------------------------------------------------
# get_token_summary
# ---------------------------------------------------------------------------

def test_get_token_summary_empty():
    result = tr.get_token_summary()
    assert result["current_month"]["cost_usd"] == 0.0
    assert result["by_activity"] == []


def test_get_token_summary_aggregates_current_month():
    tr.log_activity("score_run", detail={
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "model": "claude-sonnet-4-6",
    })
    result = tr.get_token_summary()
    assert result["current_month"]["input_tokens"] == 1_000_000
    assert result["current_month"]["cost_usd"] == pytest.approx(18.0)


def test_get_token_summary_budget_remaining():
    import os
    os.environ["MONTHLY_BUDGET_USD"] = "50"
    tr.log_activity("score_run", detail={
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "model": "claude-sonnet-4-6",
    })
    result = tr.get_token_summary()
    assert result["current_month"]["remaining_usd"] == pytest.approx(50.0 - 3.0)
