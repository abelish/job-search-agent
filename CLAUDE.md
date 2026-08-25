# job-search-agent

## WHAT

A local, CLI-driven system that automates the job search pipeline end to end:

1. Pull job postings from Greenhouse, Lever, Ashby, and SmartRecruiters company career APIs, the Bundesagentur für Arbeit job board, parsed LinkedIn and Indeed job alert emails (via Gmail), or add a single posting manually for jobs found elsewhere — by URL, or typed in by hand for postings with no URL (e.g. relayed through a recruiter email).
2. Score each posting against a stored profile for fit (skills, seniority, comp floor, location).
3. Draft a tailored resume and cover letter for postings that pass the fit threshold.
4. Track every application through a SQLite database (drafted, submitted, interviewing, rejected, offer).
5. Generate an interview prep brief (likely questions, company research) once a job moves to "interviewing."

The system drafts. The user reviews and submits manually. No bot ever clicks submit on a job board, since this violates LinkedIn and Indeed terms of service and risks account bans.

## WHY

Job searching at scale is repetitive but each application still needs to be genuinely tailored to land. The goal is to remove the repetitive overhead (finding postings, first-pass tailoring, tracking status) so time goes toward the parts that need a human (final review, interview prep, negotiation).

Secondary goal: this codebase should be clean enough to extend into a real product later. Write it like something other people might use, not just a personal script.

## HOW

### Stack
- Python 3.11+
- SQLite for the application tracker (no external DB needed for local use)
- `requests` for Greenhouse/Lever/Ashby/SmartRecruiters/Bundesagentur API calls
- Gmail API (read-only scope) for parsing LinkedIn/Indeed job alert emails
- Claude API for scoring, tailoring, and prep generation (called only where judgment is needed, not for deterministic steps like parsing)
- FastAPI + Uvicorn for the local web dashboard

### Structure
- `agents/aggregator/` — pulls and normalizes job postings from all sources into one schema; caches to `data/jobs/latest.json`
- `agents/scorer/` — hard filter (`passes_hard_filters`) runs at scan time; Claude scoring runs separately via `score_all`
- `agents/resume_tailor/` — rewrites resume bullets/summary per posting
- `agents/cover_letter/` — drafts cover letter / "why this company" per posting
- `agents/interview_prep/` — generates prep briefs once status hits "interviewing"
- `tracker/` — SQLite schema and access layer, the source of truth for application state
- `server/` — FastAPI app and static dashboard; exposes action endpoints (`/api/scan`, `/api/jobs/manual`, `/api/score`, `/api/jobs/{id}/generate-draft`, `/api/jobs/{id}/generate-prep`) so all pipeline steps can be triggered from the UI
- `cli/` — entry point: `jobsearch scan`, `jobsearch score`, `jobsearch list [--status]`, `jobsearch draft <id>`, `jobsearch prep <id>`, `jobsearch track <id> <status>`, `jobsearch readjust`, `jobsearch serve`
- `data/profile/` — resume, target roles, comp floor, location constraints
- `data/jobs/` — local cache of normalized job postings (gitignored, not committed)

### Conventions
- No hyphens or semicolons in any generated text output (resumes, cover letters, prep docs). Direct, evidence-driven prose.
- Each agent is a standalone module with a clear input/output contract so agents can be tested or swapped independently.
- Claude API calls go through a single shared client wrapper (`agents/claude_client.py`) so prompts and model choice are managed in one place, not scattered per agent.
- Deterministic work (API calls, parsing, DB writes) stays in plain Python. Claude is called only for the steps that genuinely need language judgment: scoring rationale, tailoring, drafting, prep generation.

### Anti-patterns to avoid
- Do not attempt to automate actual form submission on LinkedIn or Indeed. Draft only.
- Do not let CLAUDE.md grow past ~200 lines. It gets injected into every request. If this file is getting long, move detail into module-level README files instead.
- Do not hardcode API keys or credentials anywhere in this repo. Use environment variables and keep `.env` gitignored.
