"""
FastAPI server for the job search dashboard.

Run with:
  uvicorn server.app:app --reload

Or via CLI:
  python -m cli.main serve
"""

import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tracker import (
    get_scan_dedup_keys,
    get_job,
    get_stats,
    get_token_summary,
    init_db,
    list_activity,
    list_jobs,
    log_activity,
    update_status,
    upsert_job,
    VALID_STATUSES,
)

STATIC_DIR = Path(__file__).parent / "static"
PROFILE_PATH = Path("data/profile/profile.json")
RESUME_PATH = Path("data/profile/resume.txt")

app = FastAPI(title="Job Search Dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_score_lock = threading.Lock()
_score_state: dict = {"running": False, "total": 0, "scored": 0, "stop_requested": False}


@app.on_event("startup")
def startup():
    init_db()
    _score_state["running"] = False


def _load_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text())


# --- pages ---

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


# --- API ---

@app.get("/api/stats")
def api_stats():
    return get_stats()


@app.get("/api/jobs")
def api_list_jobs(status: str = None):
    return list_jobs(status=status)


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class StatusUpdate(BaseModel):
    status: str


@app.post("/api/jobs/{job_id}/status")
def api_update_status(job_id: str, body: StatusUpdate):
    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Valid: {sorted(VALID_STATUSES)}")
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    update_status(job_id, body.status)
    return {"ok": True}


class DraftUpdate(BaseModel):
    resume_draft: str | None = None
    cover_letter_draft: str | None = None


@app.post("/api/jobs/{job_id}/draft")
def api_update_draft(job_id: str, body: DraftUpdate):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    changed = {}
    if body.resume_draft is not None:
        job["resume_draft"] = body.resume_draft
        changed["resume_draft"] = True
    if body.cover_letter_draft is not None:
        job["cover_letter_draft"] = body.cover_letter_draft
        changed["cover_letter_draft"] = True
    if changed:
        upsert_job(job)
        log_activity("draft_generated", job_id=job_id, detail={"edited_fields": list(changed.keys()), "source": "manual_edit"})
    return {"ok": True}


@app.get("/api/activity")
def api_activity(limit: int = 100):
    return list_activity(limit=limit)


@app.get("/api/token-summary")
def api_token_summary():
    return get_token_summary()


# --- actions ---

@app.post("/api/scan")
def api_scan():
    """Fetch fresh postings from all sources and store those that pass hard filters."""
    from agents import aggregator
    from agents.scorer import passes_hard_filters
    profile = _load_profile()
    postings, warnings = aggregator.run_all()
    existing_ids, existing_urls, recent_tcs = get_scan_dedup_keys(recency_days=30)
    accepted = []
    for posting in postings:
        if posting["id"] in existing_ids or posting.get("url") in existing_urls:
            continue
        tcs_key = (posting.get("title", "").strip().lower(), (posting.get("company") or "").strip().lower(), posting.get("source", ""))
        if tcs_key in recent_tcs:
            continue
        if passes_hard_filters(posting, profile):
            upsert_job(posting)
            accepted.append(posting)
    sources = list({p["source"] for p in accepted})
    log_activity("scan_run", detail={"fetched": len(postings), "stored": len(accepted), "sources": sources})
    return {"fetched": len(postings), "stored": len(accepted), "new_in_db": len(list_jobs(status="new")), "warnings": warnings}


@app.get("/api/score/estimate")
def api_score_estimate():
    """Return count of new jobs and estimated Claude cost to score them."""
    from agents.claude_client import cost_usd
    new_jobs = list_jobs(status="new")
    n = len(new_jobs)
    est = cost_usd("claude-sonnet-4-6", n * 1200, n * 150)
    return {"new_jobs": n, "estimated_cost_usd": round(est, 4)}


@app.get("/api/score/progress")
def api_score_progress():
    """Return current scoring progress."""
    with _score_lock:
        return dict(_score_state)


@app.post("/api/score/stop")
def api_score_stop():
    """Request the in-progress scoring run to stop after the current job."""
    with _score_lock:
        if not _score_state.get("running"):
            raise HTTPException(status_code=409, detail="No scoring run in progress.")
        _score_state["stop_requested"] = True
    return {"ok": True}


@app.post("/api/score")
def api_score():
    """Start scoring all new jobs in a background thread. Returns immediately."""
    from agents import scorer
    global _score_state

    with _score_lock:
        if _score_state.get("running"):
            raise HTTPException(status_code=409, detail="Scoring already in progress.")
        new_jobs = list_jobs(status="new")
        if not new_jobs:
            return {"started": False, "total": 0}
        _score_state = {"running": True, "total": len(new_jobs), "scored": 0, "stop_requested": False}

    profile = _load_profile()

    def _run():
        def _on_progress():
            with _score_lock:
                _score_state["scored"] += 1

        def _should_stop():
            with _score_lock:
                return _score_state.get("stop_requested", False)

        try:
            above, below = scorer.score_all(new_jobs, profile, on_progress=_on_progress, should_stop=_should_stop)
            for job in above:
                upsert_job(job)
                update_status(job["id"], "scored")
            for job in below:
                upsert_job(job)
                update_status(job["id"], "scored")
        finally:
            with _score_lock:
                _score_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "total": len(new_jobs)}


@app.post("/api/jobs/{job_id}/generate-draft")
def api_generate_draft(job_id: str):
    """Generate tailored resume and cover letter for a job. Job must be scored with no existing draft."""
    from agents import resume_tailor, cover_letter as cl_agent
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") not in {"scored", "drafted"}:
        raise HTTPException(status_code=400, detail="Job must be in 'scored' status to generate a draft.")
    if job.get("resume_draft") or job.get("cover_letter_draft"):
        raise HTTPException(status_code=409, detail="Draft already exists. Edit it manually or clear it first.")
    profile = _load_profile()
    resume_text = RESUME_PATH.read_text()
    resume_draft, resume_usage = resume_tailor.tailor_resume(job, resume_text)
    cl_draft, cl_usage = cl_agent.draft_cover_letter(job, profile)
    job["resume_draft"] = resume_draft
    job["cover_letter_draft"] = cl_draft
    upsert_job(job)
    update_status(job_id, "drafted")
    total_input = resume_usage["input_tokens"] + cl_usage["input_tokens"]
    total_output = resume_usage["output_tokens"] + cl_usage["output_tokens"]
    log_activity("draft_generated", job_id=job_id, detail={
        "edited_fields": ["resume_draft", "cover_letter_draft"],
        "source": "ui",
        "input_tokens": total_input,
        "output_tokens": total_output,
        "model": resume_usage["model"],
    })
    return get_job(job_id)


@app.post("/api/jobs/{job_id}/generate-prep")
def api_generate_prep(job_id: str):
    """Generate an interview prep brief. Job must be in 'interviewing' status with no existing prep."""
    from agents import interview_prep
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "interviewing":
        raise HTTPException(status_code=400, detail="Job must be in 'interviewing' status to generate a prep brief.")
    if job.get("prep_brief"):
        raise HTTPException(status_code=409, detail="Prep brief already exists.")
    profile = _load_profile()
    brief, usage = interview_prep.generate_prep_brief(job, profile)
    job["prep_brief"] = brief
    upsert_job(job)
    log_activity("prep_generated", job_id=job_id, detail={
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "model": usage["model"],
    })
    return get_job(job_id)
