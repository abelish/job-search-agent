# job-search-agent

A local system for finding, scoring, and tailoring job applications, then tracking them through to offer.

## How this works

1. **Scan** — pulls fresh postings from Greenhouse, Lever, Ashby, SmartRecruiters, Bundesagentur, and parsed Gmail job alerts (LinkedIn, Indeed). Only postings that pass your hard filters (title keywords, excluded companies, location) are stored. Run via `jobsearch scan` or the Scan button in the dashboard. Found something outside those sources? Use **Add job by URL** in the dashboard to pull in a single posting by pasting its link — it auto-fills what it can from the page, and you can fill in or correct the rest before saving. Manually added jobs skip the hard filters and flow into the same Score → Draft → Track pipeline as scanned ones.
2. **Score** — Claude rates each new posting against your profile (0–100) with a rationale. Postings above the threshold (default 70) surface in your review queue; below-threshold postings are still visible in the dashboard with their real score so you can see why they didn't make the cut. Scoring runs in the background with a live progress bar and a Stop button that preserves already-scored results. Run via `jobsearch score [--limit N]` or the Score button in the dashboard.
3. **Dismiss or draft** — from a scored job's detail view, either dismiss it (removes it from the queue, keeps it findable under the Dismissed filter) or generate a tailored resume and cover letter via the Draft button.
4. **Review and submit manually** — open the draft in the dashboard, edit as needed, then submit on the company's career page. The system never submits on your behalf.
5. **Track status** — update the job status (submitted → interviewing → offer/rejected) via the dropdown in the dashboard or `jobsearch track <job_id> <status>`.
6. **Interview prep** — once a job is marked interviewing, generate a prep brief with likely questions and company research from the dashboard or via `jobsearch prep <job_id>`.
7. **Dashboard** — `jobsearch serve` starts a local web UI at `http://localhost:8000` that ties all of the above together.

No part of this system submits applications automatically. That step stays manual by design.

## Getting started

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/) — required for scoring, tailoring, and prep generation. Not required just to browse the dashboard or run a scan against a source that's already configured.
- Optional, only if you want a given posting source: Gmail API credentials (for LinkedIn/Indeed email parsing), Greenhouse/Lever/Ashby/SmartRecruiters board slugs, or a Bundesagentur für Arbeit API key. Every source is opt-in — leave its env vars blank and `scan` simply skips it.

### 1. Clone and install

```bash
git clone <this repo>
cd job-search-agent
python -m venv venv
```

Activate the virtualenv:

```bash
# macOS / Linux
source venv/bin/activate

# Windows (PowerShell)
venv\Scripts\Activate.ps1
```

```bash
pip install -e .
```

This installs the `jobsearch` CLI command via the `console_scripts` entry point in `pyproject.toml`. Confirm it worked:

```bash
jobsearch --help
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Variable | Required for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | scoring, tailoring, cover letters, prep briefs | from console.anthropic.com |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | LinkedIn/Indeed email parsing | see below to obtain the refresh token |
| `GREENHOUSE_BOARDS` / `LEVER_BOARDS` / `ASHBY_BOARDS` / `SMARTRECRUITERS_COMPANIES` | those ATS sources | comma-separated board/company slugs, e.g. `GREENHOUSE_BOARDS=stripe,anthropic,figma` |
| `ARBEITSAGENTUR_LOCATION` / `ARBEITSAGENTUR_API_KEY` | Bundesagentur für Arbeit source | see `.env.example` for how to grab the API key from DevTools |
| `MONTHLY_BUDGET_USD` | dashboard budget widget | optional, defaults to 50 |

To get a Gmail refresh token (only needed if you want LinkedIn/Indeed alert emails parsed):

1. In Google Cloud Console, create a project and enable the Gmail API.
2. Create an OAuth 2.0 client ID of type "Desktop app" and copy its client ID/secret into `.env`.
3. Run `python get_gmail_token.py` — it opens a browser for you to sign in, then prints the refresh token to paste into `.env`.

### 3. Set up your profile

`data/profile/profile.json` and `data/profile/resume.txt` hold your personal details and base resume. Both are gitignored, since they're private — nothing you put there ever gets committed. Create them from the checked-in templates:

```bash
cp data/profile/profile.example.json data/profile/profile.json
cp data/profile/resume.example.txt data/profile/resume.txt
```

Edit `profile.json` with your own details — target titles, years of experience, comp floor, location preferences, companies/keywords to exclude, and must-have title keywords. This is what every posting gets scored against, so the more specific, the better the fit scores will be. See `ARCHITECTURE.md` for the full field reference.

Fill in `resume.txt` with your own base resume as plain text, following the section structure in the example. The resume tailor agent only ever re-emphasizes and re-orders content from this file, so it needs to already contain everything you want it to be able to draw from.

### 4. Run it

```bash
jobsearch scan             # pull fresh postings from every configured source
jobsearch score            # score new postings against your profile (calls Claude)
jobsearch list --status scored
jobsearch draft <job_id>   # generate a tailored resume + cover letter
jobsearch track <job_id> submitted
jobsearch prep <job_id>    # once a job is marked interviewing
jobsearch serve            # dashboard at http://localhost:8000
```

Or skip the CLI entirely and drive the whole pipeline from `jobsearch serve` — the dashboard's Pipeline view has buttons for scan, score, and per-job draft/prep generation.

### 5. Run the tests

```bash
pytest
```

## Project layout

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full system overview, data flow, module reference, and API/CLI documentation. `CLAUDE.md` covers the project's goals and conventions.

## Roadmap

- [x] Greenhouse/Lever/Ashby aggregator
- [x] Gmail job alert parser
- [x] Fit scorer against profile
- [x] Resume tailor agent
- [x] Cover letter agent
- [x] SQLite tracker schema and CLI commands
- [x] Interview prep agent
- [x] Web dashboard
- [x] Manual job entry by URL
- [x] Packaged as installable CLI tool
- [x] Tests
