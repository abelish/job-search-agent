"""
Application tracker.

SQLite database that is the source of truth for application state.
Lives at tracker/jobsearch.db (gitignored, local only).

Schema:

jobs
----
id            TEXT PRIMARY KEY   -- matches aggregator's normalized id
source        TEXT
title         TEXT
company       TEXT
location      TEXT
url           TEXT
description   TEXT
posted_date   TEXT
fetched_date  TEXT
fit_score     INTEGER
fit_rationale TEXT
status        TEXT    -- new | scored | drafted | submitted | interviewing | rejected | offer
resume_draft  TEXT
cover_letter_draft TEXT
prep_brief    TEXT
last_updated  TEXT

activity_log
------------
id            INTEGER PRIMARY KEY AUTOINCREMENT
event_type    TEXT    -- status_change | draft_generated | scan_run
job_id        TEXT    -- NULL for scan_run events
detail        TEXT    -- JSON blob with event-specific fields
created_at    TEXT    -- ISO 8601
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "jobsearch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT,
    title TEXT,
    company TEXT,
    location TEXT,
    url TEXT,
    description TEXT,
    posted_date TEXT,
    fetched_date TEXT,
    fit_score INTEGER,
    fit_rationale TEXT,
    status TEXT DEFAULT 'new',
    resume_draft TEXT,
    cover_letter_draft TEXT,
    prep_brief TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    job_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""

VALID_STATUSES = {"new", "dismissed", "scored", "drafted", "submitted", "interviewing", "rejected", "offer"}

# Prices in USD per million tokens — kept in sync with agents/claude_client.py
_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    return {col[0]: row[col[0]] for col in cursor.description}


def _token_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = _PRICING.get(model, _PRICING["claude-sonnet-4-6"])
    return (input_tokens / 1_000_000 * prices["input"]) + (output_tokens / 1_000_000 * prices["output"])


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def log_activity(event_type: str, job_id: str | None = None, detail: dict | None = None):
    """
    Append a row to activity_log.
    event_type: "status_change" | "draft_generated" | "scan_run"
    To track token usage, include input_tokens, output_tokens, and model in detail.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO activity_log (event_type, job_id, detail, created_at) VALUES (?, ?, ?, ?)",
        (event_type, job_id, json.dumps(detail or {}), _now()),
    )
    conn.commit()
    conn.close()


def upsert_job(job: dict):
    """Insert or update a job row from a normalized posting dict."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO jobs (
            id, source, title, company, location, url, description,
            posted_date, fetched_date, fit_score, fit_rationale, status,
            resume_draft, cover_letter_draft, prep_brief, last_updated
        ) VALUES (
            :id, :source, :title, :company, :location, :url, :description,
            :posted_date, :fetched_date, :fit_score, :fit_rationale, :status,
            :resume_draft, :cover_letter_draft, :prep_brief, :last_updated
        )
        ON CONFLICT(id) DO UPDATE SET
            source            = excluded.source,
            title             = excluded.title,
            company           = excluded.company,
            location          = excluded.location,
            url               = excluded.url,
            description       = excluded.description,
            posted_date       = excluded.posted_date,
            fetched_date      = excluded.fetched_date,
            fit_score         = COALESCE(excluded.fit_score, fit_score),
            fit_rationale     = COALESCE(excluded.fit_rationale, fit_rationale),
            resume_draft      = COALESCE(excluded.resume_draft, resume_draft),
            cover_letter_draft = COALESCE(excluded.cover_letter_draft, cover_letter_draft),
            prep_brief        = COALESCE(excluded.prep_brief, prep_brief),
            last_updated      = excluded.last_updated
        """,
        {
            "id": job["id"],
            "source": job.get("source"),
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
            "url": job.get("url"),
            "description": job.get("description"),
            "posted_date": job.get("posted_date"),
            "fetched_date": job.get("fetched_date"),
            "fit_score": job.get("fit_score"),
            "fit_rationale": job.get("fit_rationale"),
            "status": job.get("status", "new"),
            "resume_draft": job.get("resume_draft"),
            "cover_letter_draft": job.get("cover_letter_draft"),
            "prep_brief": job.get("prep_brief"),
            "last_updated": _now(),
        },
    )
    conn.commit()
    conn.close()


def update_status(job_id: str, status: str):
    """Update status and last_updated for a given job id. Logs the change."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid: {VALID_STATUSES}")
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    previous = row[0] if row else None
    if status == "dismissed":
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    else:
        conn.execute(
            "UPDATE jobs SET status = ?, last_updated = ? WHERE id = ?",
            (status, _now(), job_id),
        )
    conn.commit()
    conn.close()
    log_activity("status_change", job_id=job_id, detail={"from": previous, "to": status})


def get_job(job_id: str) -> dict | None:
    """Fetch a single job row as a dict, or None if not found."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_scan_dedup_keys(recency_days: int = 30) -> tuple[set[str], set[str], set[tuple]]:
    """
    Return three sets used to skip already-known jobs during a scan:
      id_set   — all job IDs in the DB (any age)
      url_set  — all job URLs in the DB (any age)
      tcs_set  — (title_lower, company_lower, source) tuples for jobs fetched
                 within the last recency_days days; blocks re-import of the same
                 role reposted with a new ID (e.g. multiple LinkedIn listings)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=recency_days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, url, title, company, source, fetched_date FROM jobs").fetchall()
    conn.close()
    ids: set[str] = set()
    urls: set[str] = set()
    tcs: set[tuple] = set()
    for r in rows:
        ids.add(r[0])
        if r[1]:
            urls.add(r[1])
        if (r[5] or "") >= cutoff and r[2] and r[4]:
            tcs.add((r[2].strip().lower(), (r[3] or "").strip().lower(), r[4]))
    return ids, urls, tcs


def list_jobs(status: str = None) -> list[dict]:
    """Fetch all jobs, optionally filtered by status, newest first."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if status:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY fetched_date DESC, id DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY fetched_date DESC, id DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_activity(limit: int = 100) -> list[dict]:
    """Fetch recent activity log entries, newest first."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    results = []
    for r in rows:
        entry = dict(r)
        entry["detail"] = json.loads(entry["detail"] or "{}")
        results.append(entry)
    return results


def get_stats() -> dict:
    """Return counts per status for the pipeline summary."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
    ).fetchall()
    conn.close()
    counts = {r[0]: r[1] for r in rows}
    return {s: counts.get(s, 0) for s in ["new", "dismissed", "scored", "drafted", "submitted", "interviewing", "rejected", "offer"]}


def get_token_summary() -> dict:
    """
    Aggregate token usage from activity_log entries that have token data
    (detail must include input_tokens and output_tokens).

    Returns:
      current_month: monthly totals, cost, budget, and remaining
      by_activity:   per-entry breakdown newest first
    """
    now = datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m")
    budget = float(os.environ.get("MONTHLY_BUDGET_USD", "50.0"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM activity_log WHERE detail LIKE '%\"input_tokens\"%' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

    total_input = 0
    total_output = 0
    total_cost = 0.0
    by_activity = []

    for r in rows:
        entry = dict(r)
        detail = json.loads(entry["detail"] or "{}")
        if "input_tokens" not in detail:
            continue
        input_tokens = int(detail.get("input_tokens", 0))
        output_tokens = int(detail.get("output_tokens", 0))
        model = detail.get("model", "claude-sonnet-4-6")
        cost = _token_cost(model, input_tokens, output_tokens)

        by_activity.append({
            "id": entry["id"],
            "event_type": entry["event_type"],
            "job_id": entry["job_id"],
            "created_at": entry["created_at"],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 6),
            "model": model,
        })

        if entry["created_at"].startswith(month_prefix):
            total_input += input_tokens
            total_output += output_tokens
            total_cost += cost

    total_cost = round(total_cost, 4)
    remaining = round(max(0.0, budget - total_cost), 4)
    pct_used = round(total_cost / budget * 100, 1) if budget > 0 else 0.0

    return {
        "current_month": {
            "month": month_prefix,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cost_usd": total_cost,
            "budget_usd": budget,
            "remaining_usd": remaining,
            "pct_used": pct_used,
        },
        "by_activity": by_activity,
    }
