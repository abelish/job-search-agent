# job-search-agent

A local system for finding, scoring, and tailoring job applications, then tracking them through to offer.

## How this works

1. **Scan** — pulls fresh postings from Greenhouse, Lever, Ashby, SmartRecruiters, Bundesagentur, and parsed Gmail job alerts (LinkedIn, Indeed). Only postings that pass your hard filters (title keywords, excluded companies, location) are stored. Run via `jobsearch scan` or the Scan button in the dashboard.
2. **Score** — Claude rates each new posting against your profile (0–100) with a rationale. Postings above the threshold (default 70) surface in your review queue; below-threshold postings are still visible in the dashboard with their real score so you can see why they didn't make the cut. Scoring runs in the background with a live progress bar and a Stop button that preserves already-scored results. Run via `jobsearch score [--limit N]` or the Score button in the dashboard.
3. **Dismiss or draft** — from a scored job's detail view, either dismiss it (removes it from the queue, keeps it findable under the Dismissed filter) or generate a tailored resume and cover letter via the Draft button.
4. **Review and submit manually** — open the draft in the dashboard, edit as needed, then submit on the company's career page. The system never submits on your behalf.
5. **Track status** — update the job status (submitted → interviewing → offer/rejected) via the dropdown in the dashboard or `jobsearch track <job_id> <status>`.
6. **Interview prep** — once a job is marked interviewing, generate a prep brief with likely questions and company research from the dashboard or via `jobsearch prep <job_id>`.
7. **Dashboard** — `jobsearch serve` starts a local web UI at `http://localhost:8000` that ties all of the above together.

No part of this system submits applications automatically. That step stays manual by design.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -e .
cp .env.example .env
# fill in .env with your own credentials
```

Fill out `data/profile/profile.json` with your details (target roles, comp floor, location, skills) and paste your base resume into `data/profile/resume.txt` before running a scan.

## Project layout

See `CLAUDE.md` for the full architecture writeup.

## Roadmap

- [x] Greenhouse/Lever/Ashby aggregator
- [x] Gmail job alert parser
- [x] Fit scorer against profile
- [x] Resume tailor agent
- [x] Cover letter agent
- [x] SQLite tracker schema and CLI commands
- [x] Interview prep agent
- [x] Web dashboard
- [x] Packaged as installable CLI tool
- [x] Tests
