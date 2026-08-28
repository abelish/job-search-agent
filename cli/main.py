"""
CLI entry point for the job search agent.

Commands:
  jobsearch scan              pull fresh postings from all configured sources
  jobsearch score             score all new postings against the profile
  jobsearch list [--status]   list tracked jobs, optionally filtered by status
  jobsearch draft <job_id>    generate tailored resume + cover letter for a job
  jobsearch track <job_id> <status>   update a job's status
  jobsearch prep <job_id>     generate an interview prep brief
  jobsearch serve             start the web dashboard (default: http://localhost:8000)

Run as: python -m cli.main <command> [args]
Once stable, package with a console_scripts entry point so it runs as
just `jobsearch` after `pip install -e .`.
"""

import json
import sys
import click
from dotenv import load_dotenv

load_dotenv()

# Windows consoles default to a codepage (e.g. cp1252) that can't encode every
# character a scraped job title/location might contain. Without this, printing
# one crashes the whole command instead of just garbling that one character.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

from agents import aggregator, scorer, resume_tailor, cover_letter, interview_prep
from tracker import init_db, upsert_job, update_status, update_fit_score, get_job, list_jobs, log_activity, get_scan_dedup_keys

PROFILE_PATH = "data/profile/profile.json"
RESUME_PATH = "data/profile/resume.txt"


def load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return json.load(f)


@click.group()
def cli():
    init_db()


@cli.command()
def scan():
    """Pull fresh postings from all configured sources."""
    profile = load_profile()
    postings, warnings = aggregator.run_all()
    existing_ids, existing_urls, recent_tcs = get_scan_dedup_keys(recency_days=30)
    accepted = []
    for posting in postings:
        if posting["id"] in existing_ids or posting.get("url") in existing_urls:
            continue
        tcs_key = (posting.get("title", "").strip().lower(), (posting.get("company") or "").strip().lower(), posting.get("source", ""))
        if tcs_key in recent_tcs:
            continue
        if scorer.passes_hard_filters(posting, profile):
            upsert_job(posting)
            accepted.append(posting)
    sources = list({p["source"] for p in accepted})
    log_activity("scan_run", detail={"fetched": len(postings), "stored": len(accepted), "sources": sources})
    click.echo(f"Fetched {len(postings)} postings, stored {len(accepted)} that pass filters.")
    for w in warnings:
        click.echo(f"  Warning: {w}", err=True)


@cli.command()
@click.option("--limit", default=None, type=int, help="Cap number of postings to score (for testing).")
def score(limit):
    """Score all new postings against the profile."""
    profile = load_profile()
    new_jobs = list_jobs(status="new")
    if not new_jobs:
        click.echo("No new postings to score.")
        return
    if limit:
        new_jobs = new_jobs[:limit]
        click.echo(f"Limiting to {limit} postings.")
    above, below, filtered = scorer.score_all(new_jobs, profile)
    for job in above:
        upsert_job(job)
        update_status(job["id"], "scored")
    for job in below:
        upsert_job(job)
        update_status(job["id"], "scored")
    for job in filtered:
        upsert_job(job)
        update_status(job["id"], "filtered")
    click.echo(f"Scored {len(new_jobs)} postings: {len(above)} above threshold, {len(below)} below, {len(filtered)} filtered out before scoring.")


@cli.command(name="list")
@click.option("--status", default=None, help="Filter by status")
def list_cmd(status):
    """List tracked jobs."""
    jobs = list_jobs(status=status)
    for job in jobs:
        click.echo(f"{job['id']}  {job['company']}  {job['title']}  [{job['status']}]")


@cli.command()
@click.argument("job_id")
def draft(job_id):
    """Generate tailored resume and cover letter for a job."""
    job = get_job(job_id)
    if not job:
        raise click.ClickException(f"Job {job_id} not found.")
    profile = load_profile()
    with open(RESUME_PATH) as f:
        resume_text = f.read()

    resume_draft, resume_usage = resume_tailor.tailor_resume(job, resume_text)
    cl_draft, cl_usage = cover_letter.draft_cover_letter(job, profile)

    job["resume_draft"] = resume_draft
    job["cover_letter_draft"] = cl_draft
    upsert_job(job)
    update_status(job_id, "drafted")

    total_input = resume_usage["input_tokens"] + cl_usage["input_tokens"]
    total_output = resume_usage["output_tokens"] + cl_usage["output_tokens"]
    log_activity("draft_generated", job_id=job_id, detail={
        "edited_fields": ["resume_draft", "cover_letter_draft"],
        "source": "cli",
        "input_tokens": total_input,
        "output_tokens": total_output,
        "model": resume_usage["model"],
    })
    click.echo(f"Draft ready for {job_id}.")


@cli.command()
@click.argument("job_id")
@click.argument("status")
def track(job_id, status):
    """Update a job's status."""
    update_status(job_id, status)
    click.echo(f"{job_id} marked as {status}.")


@cli.command()
@click.argument("job_id")
def prep(job_id):
    """Generate an interview prep brief."""
    job = get_job(job_id)
    if not job:
        raise click.ClickException(f"Job {job_id} not found.")
    profile = load_profile()
    brief, usage = interview_prep.generate_prep_brief(job, profile)
    job["prep_brief"] = brief
    upsert_job(job)
    log_activity("prep_generated", job_id=job_id, detail={
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "model": usage["model"],
    })
    click.echo(brief)


@cli.command()
def readjust():
    """Re-apply score adjustments to already-scored jobs without calling Claude.

    Strips any existing location boost or manager-level penalty from each
    scored job's rationale, recovers the raw Claude score, then re-applies
    the profile's current locations.priority_location_terms/boost/penalty
    config. Only jobs whose score or rationale changes are written back to
    the database. Run this after editing that config in profile.json.
    """
    profile = load_profile()
    all_jobs = list_jobs()
    scored = [j for j in all_jobs if j.get("fit_score") is not None]
    if not scored:
        click.echo("No scored jobs found.")
        return

    updated = scorer.reapply_adjustments(scored, profile)
    for job in updated:
        update_fit_score(job["id"], job["fit_score"], job["fit_rationale"])
        click.echo(f"  {job['company']} - {job['title']} ({job['location']}): score -> {job['fit_score']}")

    click.echo(f"\nUpdated {len(updated)} of {len(scored)} scored job(s).")


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True)
@click.option("--reload", is_flag=True, default=False, help="Auto-reload on code changes (dev mode)")
def serve(host, port, reload):
    """Start the web dashboard."""
    import uvicorn
    click.echo(f"Dashboard at http://{host}:{port}")
    uvicorn.run("server.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    cli()
