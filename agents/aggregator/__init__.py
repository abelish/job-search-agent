"""
Aggregator agent.

Pulls job postings from:
- Greenhouse job board API (public, per company board token)
- Lever job board API (public, per company)
- Ashby job board API (public, per company)
- Gmail job alert emails (LinkedIn, Indeed) via Gmail API, read-only scope

Normalizes everything into one schema:

{
    "id": str,            # stable sha1 of source + url (12 hex chars)
    "source": str,        # "greenhouse" | "lever" | "ashby" | "linkedin_email" | "indeed_email"
    "title": str,
    "company": str,
    "location": str,
    "url": str,
    "description": str,
    "posted_date": str,   # ISO 8601 date (YYYY-MM-DD)
    "fetched_date": str,  # ISO 8601 UTC timestamp
}

Configure sources via env vars (comma-separated board/company slugs):
  GREENHOUSE_BOARDS=stripe,anthropic,figma
  LEVER_BOARDS=vercel,linear
  ASHBY_BOARDS=ramp,retool
  GMAIL_CLIENT_ID=...
  GMAIL_CLIENT_SECRET=...
  GMAIL_REFRESH_TOKEN=...
"""

import base64
import hashlib
import html as _html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

JOBS_CACHE = Path("data/jobs/latest.json")


# --- HTML utilities ---

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    def get_text(self) -> str:
        return " ".join(self._parts)


class _LinkExtractor(HTMLParser):
    """Extracts (href, link_text) pairs from HTML."""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href", "")
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._buf).strip()))
            self._href = None
            self._buf = []

    def handle_data(self, data: str):
        if self._href is not None:
            self._buf.append(data.strip())


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.get_text()


# --- Shared helpers ---

def _make_id(source: str, url: str) -> str:
    return hashlib.sha1(f"{source}:{url}".encode()).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Greenhouse ---

def fetch_greenhouse(board_token: str) -> list[dict]:
    """
    Greenhouse public job board API.
    https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
    """
    base = "https://boards-api.greenhouse.io/v1/boards"

    try:
        board_resp = requests.get(f"{base}/{board_token}", timeout=10)
        board_resp.raise_for_status()
        company = board_resp.json().get("name", board_token)
    except Exception:
        company = board_token.replace("-", " ").title()

    resp = requests.get(f"{base}/{board_token}/jobs?content=true", timeout=30)
    resp.raise_for_status()

    fetched = _now()
    postings = []
    for job in resp.json().get("jobs", []):
        url = job.get("absolute_url", "")
        if not url:
            continue
        location = (job.get("location") or {}).get("name", "")
        posted = (job.get("updated_at") or "")[:10]
        postings.append({
            "id": _make_id("greenhouse", url),
            "source": "greenhouse",
            "title": job.get("title", ""),
            "company": company,
            "location": location,
            "url": url,
            "description": _html_to_text(job.get("content", "")),
            "posted_date": posted,
            "fetched_date": fetched,
        })
    return postings


# --- Lever ---

def fetch_lever(company: str) -> list[dict]:
    """
    Lever public job board API.
    https://api.lever.co/v0/postings/{company}?mode=json
    """
    resp = requests.get(
        f"https://api.lever.co/v0/postings/{company}?mode=json",
        timeout=30,
    )
    resp.raise_for_status()

    company_display = company.replace("-", " ").title()
    fetched = _now()
    postings = []
    for job in resp.json():
        url = job.get("hostedUrl", "")
        if not url:
            continue
        categories = job.get("categories") or {}
        location = categories.get("location", "")
        description = job.get("descriptionPlain") or _html_to_text(job.get("description", ""))
        created_ms = job.get("createdAt") or 0
        posted = (
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).date().isoformat()
            if created_ms else ""
        )
        postings.append({
            "id": _make_id("lever", url),
            "source": "lever",
            "title": job.get("text", ""),
            "company": company_display,
            "location": location,
            "url": url,
            "description": description,
            "posted_date": posted,
            "fetched_date": fetched,
        })
    return postings


# --- Ashby ---

def fetch_ashby(job_board_name: str) -> list[dict]:
    """
    Ashby public job board API.
    https://api.ashbyhq.com/posting-api/job-board/{job_board_name}
    """
    resp = requests.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{job_board_name}",
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    org = data.get("organization") or {}
    company = org.get("name") or job_board_name.replace("-", " ").title()
    fetched = _now()

    postings = []
    for job in data.get("jobPostings", []):
        url = job.get("jobUrl", "")
        if not url:
            continue
        postings.append({
            "id": _make_id("ashby", url),
            "source": "ashby",
            "title": job.get("title", ""),
            "company": company,
            "location": job.get("locationName", ""),
            "url": url,
            "description": _html_to_text(job.get("descriptionHtml", "")),
            "posted_date": job.get("publishedDate", ""),
            "fetched_date": fetched,
        })
    return postings


# --- Gmail ---

def _gmail_access_token() -> str:
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _gmail_search(token: str, query: str, max_results: int = 50) -> list[str]:
    resp = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        params={"q": query, "maxResults": max_results},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("messages", [])]


def _gmail_html_body(token: str, message_id: str) -> str:
    resp = requests.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
        params={"format": "full"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()

    def _extract(payload: dict) -> str:
        if payload.get("mimeType") == "text/html":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            result = _extract(part)
            if result:
                return result
        return ""

    return _extract(resp.json().get("payload", {}))


_LINKEDIN_JOB_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
_INDEED_JOB_RE = re.compile(r"indeed\.com/(?:viewjob\?jk=|rc/clk\?jk=)(\w+)")
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


_LI_DESC_RE = re.compile(
    r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)


def _fetch_description_from_page(url: str) -> str:
    """
    Fetch a public job posting page and extract the description.
    Tries JSON-LD first (Indeed, some ATS pages), then falls back to
    LinkedIn's show-more-less-html__markup div.
    Returns empty string on any failure or if nothing is found.
    """
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=15)
    resp.raise_for_status()
    html = resp.text

    # Try JSON-LD first (Indeed and others)
    for m in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                data = next((d for d in data if d.get("@type") == "JobPosting"), {})
            if data.get("@type") == "JobPosting":
                desc = data.get("description", "")
                if desc:
                    return _html_to_text(desc)
        except Exception:
            continue

    # Fallback: LinkedIn serves description in show-more-less-html__markup
    m = _LI_DESC_RE.search(html)
    if m:
        return _html_to_text(m.group(1))

    return ""
_BULLET_RE = re.compile(r"\s*(?:&middot;|&bull;|[·•\xb7�])\s*")

# Matches the bold title anchor in LinkedIn alert emails:
# <a href="...jobs/view/{ID}/..." style="...font-weight:600...">Title text</a>
_LI_TITLE_A_RE = re.compile(
    r'(<a\b[^>]*style="[^"]*font-weight:\s*600[^"]*"[^>]*>)(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)

# Matches the company/location paragraph:
# <p ...>Company &middot; Location (Work type)</p>
# LinkedIn emails use &middot; as an HTML entity in the raw MIME body.
_LI_COMPANY_LOC_RE = re.compile(
    r'<p\b[^>]*>\s*([^<\r\n]+(?:&middot;|&bull;|[·•\xb7])[^<\r\n]+?)\s*</p>',
    re.IGNORECASE,
)


def _split_gmail_job_text(text: str) -> tuple[str, str, str]:
    """
    Fallback parser for older-format job alert anchor text:
    'Title      Company • Location'
    Returns (title, company, location).
    """
    chunks = [c.strip() for c in re.split(r"\s{2,}", text.strip()) if c.strip()]
    if not chunks:
        return text.strip(), "", ""
    title = chunks[0]
    if len(chunks) == 1:
        return title, "", ""
    bullet_parts = re.split(r"\s*[•·•\xb7�]\s*", chunks[1], maxsplit=1)
    if len(bullet_parts) == 2:
        return title, bullet_parts[0].strip(), bullet_parts[1].strip()
    return title, chunks[1], ""


def _parse_linkedin_email(html: str, fetched: str) -> list[dict]:
    """
    Parse LinkedIn job alert digest emails (2024+ format).

    Titles live in <a style="...font-weight:600..."> anchors.
    Company/location live in the next <p> containing a bullet separator.
    Pairs them by document position rather than stateful parsing.
    """
    # Step 1: collect (position, job_id, title) from bold title anchors
    candidates: list[tuple[int, str, str]] = []
    for m in _LI_TITLE_A_RE.finditer(html):
        opening_tag = m.group(1)
        href_m = re.search(r'href="([^"]*)"', opening_tag)
        if not href_m:
            continue
        job_id_m = _LINKEDIN_JOB_RE.search(href_m.group(1))
        if not job_id_m:
            continue
        title = _html_to_text(m.group(2)).strip()
        if title:
            candidates.append((m.start(), job_id_m.group(1), title))

    # Step 2: collect (position, company, location) from bullet-separated <p> tags
    company_locs: list[tuple[int, str, str]] = []
    for m in _LI_COMPANY_LOC_RE.finditer(html):
        text = m.group(1).strip()
        parts = _BULLET_RE.split(text, maxsplit=1)
        company_locs.append((
            m.start(),
            parts[0].strip(),
            parts[1].strip() if len(parts) > 1 else "",
        ))

    # Step 3: pair each title with the first company/loc that follows it
    seen: set[str] = set()
    postings: list[dict] = []
    for title_pos, job_id, title in candidates:
        if job_id in seen:
            continue
        seen.add(job_id)
        company = location = ""
        for cl_pos, cl_company, cl_location in company_locs:
            if cl_pos > title_pos:
                company, location = cl_company, cl_location
                break
        url = f"https://www.linkedin.com/jobs/view/{job_id}"
        postings.append({
            "id": _make_id("linkedin_email", url),
            "source": "linkedin_email",
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "description": "",
            "posted_date": "",
            "fetched_date": fetched,
        })
    return postings


def _parse_indeed_email(html: str, fetched: str) -> list[dict]:
    parser = _LinkExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    seen: set[str] = set()
    postings = []
    for href, text in parser.links:
        m = _INDEED_JOB_RE.search(href)
        if not m or not text.strip():
            continue
        jk = m.group(1)
        if jk in seen:
            continue
        seen.add(jk)
        url = f"https://www.indeed.com/viewjob?jk={jk}"
        title, company, location = _split_gmail_job_text(text)
        postings.append({
            "id": _make_id("indeed_email", url),
            "source": "indeed_email",
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "description": "",
            "posted_date": "",
            "fetched_date": fetched,
        })
    return postings


def fetch_gmail_alerts(days_back: int = 7) -> list[dict]:
    """
    Parse LinkedIn and Indeed job alert emails from the last `days_back` days.
    Requires GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN in env.

    Note: LinkedIn and Indeed emails provide titles and URLs but not full
    descriptions. Those fields are left blank for manual review or future
    enrichment.
    """
    required = {"GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"}
    if missing := required - set(os.environ):
        raise RuntimeError(f"Gmail credentials not set: {', '.join(sorted(missing))}")

    token = _gmail_access_token()
    fetched = _now()
    after = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y/%m/%d")
    postings: list[dict] = []

    linkedin_query = f"from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com OR jobs-listings@linkedin.com) after:{after}"
    for msg_id in _gmail_search(token, linkedin_query):
        try:
            html = _gmail_html_body(token, msg_id)
            postings.extend(_parse_linkedin_email(html, fetched))
        except Exception as e:
            print(f"  Gmail LinkedIn msg {msg_id}: {e}", file=sys.stderr)

    indeed_query = f"from:(alert@indeed.com OR jobalert@indeed.com) after:{after}"
    for msg_id in _gmail_search(token, indeed_query):
        try:
            html = _gmail_html_body(token, msg_id)
            postings.extend(_parse_indeed_email(html, fetched))
        except Exception as e:
            print(f"  Gmail Indeed msg {msg_id}: {e}", file=sys.stderr)

    # Enrich Gmail-sourced postings with full descriptions from the posting page.
    # Both LinkedIn and Indeed embed JSON-LD JobPosting data on their public job pages.
    enriched = 0
    for posting in postings:
        if posting.get("description"):
            continue
        try:
            desc = _fetch_description_from_page(posting["url"])
            if desc:
                posting["description"] = desc
                enriched += 1
        except Exception as e:
            print(f"  Enrichment failed {posting.get('url', '')}: {e}", file=sys.stderr)
    if enriched:
        print(f"  Enriched {enriched}/{len(postings)} Gmail postings with descriptions", file=sys.stderr)

    return postings


# --- SmartRecruiters ---

def fetch_smartrecruiters(company: str) -> list[dict]:
    """
    SmartRecruiters public job board API. No auth required for public postings.
    List:   GET https://api.smartrecruiters.com/v1/companies/{company}/postings
    Detail: GET https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}
    """
    base = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    resp = requests.get(base, params={"limit": 100}, timeout=30)
    resp.raise_for_status()

    company_display = company.replace("-", " ").title()
    fetched = _now()
    postings = []

    for job in resp.json().get("content", []):
        job_id = job.get("id", "")
        if not job_id:
            continue
        url = f"https://careers.smartrecruiters.com/{company}/{job_id}"

        description = ""
        try:
            detail = requests.get(f"{base}/{job_id}", timeout=15)
            detail.raise_for_status()
            sections = detail.json().get("jobAd", {}).get("sections", {})
            description = _html_to_text(sections.get("jobDescription", {}).get("text", ""))
        except Exception:
            pass

        loc = job.get("location") or {}
        location_str = ", ".join(filter(None, [loc.get("city", ""), loc.get("country", "")]))

        postings.append({
            "id": _make_id("smartrecruiters", url),
            "source": "smartrecruiters",
            "title": job.get("name", ""),
            "company": company_display,
            "location": location_str,
            "url": url,
            "description": description,
            "posted_date": (job.get("releasedDate") or "")[:10],
            "fetched_date": fetched,
        })
    return postings


# --- Bundesagentur für Arbeit ---

def fetch_arbeitsagentur(location: str = "Berlin", radius_km: int = 25) -> list[dict]:
    """
    Bundesagentur für Arbeit public job search API.
    Requires ARBEITSAGENTUR_API_KEY in env. The BA API key can be obtained by
    inspecting network requests on https://www.arbeitsagentur.de/jobsuche and
    copying the X-API-Key header value.
    List:   GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs
    Detail: GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{hashId}
    """
    api_key = os.environ.get("ARBEITSAGENTUR_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ARBEITSAGENTUR_API_KEY is not set — see .env.example for instructions")
    # The BA API is a browser-facing endpoint; it requires Origin/Referer and a browser
    # User-Agent alongside the X-API-Key or it returns 400.
    headers = {
        "X-API-Key": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Origin": "https://www.arbeitsagentur.de",
        "Referer": "https://www.arbeitsagentur.de/jobsuche",
    }
    resp = requests.get(
        "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs",
        headers=headers,
        params={"wo": location, "umkreis": radius_km, "angebotsart": 1, "size": 100, "page": 1},
        timeout=30,
    )
    if not resp.ok:
        print(f"  Arbeitsagentur response body: {resp.text[:500]}", file=sys.stderr)
    resp.raise_for_status()

    fetched = _now()
    postings = []

    for job in resp.json().get("stellenangebote") or []:
        hash_id = job.get("hashId", "")
        if not hash_id:
            continue
        url = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{hash_id}"

        arbeitsort = job.get("arbeitsort") or {}
        location_str = ", ".join(filter(None, [arbeitsort.get("ort", ""), arbeitsort.get("region", "")]))

        description = ""
        try:
            detail = requests.get(
                f"https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{hash_id}",
                headers=headers,  # same browser headers as the list request
                timeout=15,
            )
            detail.raise_for_status()
            raw = detail.json()
            description = _html_to_text(
                raw.get("stellenbeschreibung") or raw.get("stellenangebotsBeschreibung") or ""
            )
        except Exception:
            pass

        postings.append({
            "id": _make_id("arbeitsagentur", url),
            "source": "arbeitsagentur",
            "title": job.get("titel", ""),
            "company": job.get("arbeitgeber", ""),
            "location": location_str,
            "url": url,
            "description": description,
            "posted_date": (job.get("aktuelleVeroeffentlichungsdatum") or "")[:10],
            "fetched_date": fetched,
        })
    return postings


# --- run_all ---

def _source_warning(label: str, exc: Exception) -> str:
    """Convert a fetch exception into a user-facing warning string."""
    msg = str(exc)
    if "400" in msg:
        return f"{label}: 400 Bad Request — check your API key value and try re-copying it from browser DevTools"
    if "401" in msg:
        return f"{label}: 401 Unauthorized — credentials rejected"
    if "403" in msg:
        return f"{label}: 403 Forbidden — API key may be invalid or expired"
    if "404" in msg:
        return f"{label}: 404 Not Found — check the board slug or company name"
    return f"{label}: {msg}"


def run_all() -> tuple[list[dict], list[str]]:
    """
    Pull from all configured sources, deduplicate by id, cache to data/jobs/latest.json.
    Sources are read from env vars:
      GREENHOUSE_BOARDS, LEVER_BOARDS, ASHBY_BOARDS, SMARTRECRUITERS_COMPANIES (comma-separated slugs)
      ARBEITSAGENTUR_LOCATION (city name, e.g. "Berlin")
      GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN

    Returns (postings, warnings) where warnings is a list of human-readable error strings
    for any sources that failed — empty list means all sources succeeded.
    """
    all_postings: list[dict] = []
    warnings: list[str] = []

    def _boards(env_key: str) -> list[str]:
        return [t.strip() for t in os.environ.get(env_key, "").split(",") if t.strip()]

    for board in _boards("GREENHOUSE_BOARDS"):
        try:
            found = fetch_greenhouse(board)
            print(f"  Greenhouse {board}: {len(found)}", file=sys.stderr)
            all_postings.extend(found)
        except Exception as e:
            w = _source_warning(f"Greenhouse {board}", e)
            print(f"  {w}", file=sys.stderr)
            warnings.append(w)

    for company in _boards("LEVER_BOARDS"):
        try:
            found = fetch_lever(company)
            print(f"  Lever {company}: {len(found)}", file=sys.stderr)
            all_postings.extend(found)
        except Exception as e:
            w = _source_warning(f"Lever {company}", e)
            print(f"  {w}", file=sys.stderr)
            warnings.append(w)

    for board in _boards("ASHBY_BOARDS"):
        try:
            found = fetch_ashby(board)
            print(f"  Ashby {board}: {len(found)}", file=sys.stderr)
            all_postings.extend(found)
        except Exception as e:
            w = _source_warning(f"Ashby {board}", e)
            print(f"  {w}", file=sys.stderr)
            warnings.append(w)

    for company in _boards("SMARTRECRUITERS_COMPANIES"):
        try:
            found = fetch_smartrecruiters(company)
            print(f"  SmartRecruiters {company}: {len(found)}", file=sys.stderr)
            all_postings.extend(found)
        except Exception as e:
            w = _source_warning(f"SmartRecruiters {company}", e)
            print(f"  {w}", file=sys.stderr)
            warnings.append(w)

    ba_location = os.environ.get("ARBEITSAGENTUR_LOCATION", "").strip()
    if ba_location:
        try:
            found = fetch_arbeitsagentur(ba_location)
            print(f"  Arbeitsagentur {ba_location}: {len(found)}", file=sys.stderr)
            all_postings.extend(found)
        except Exception as e:
            w = _source_warning(f"Arbeitsagentur {ba_location}", e)
            print(f"  {w}", file=sys.stderr)
            warnings.append(w)

    gmail_ready = all(os.environ.get(k) for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"))
    if gmail_ready:
        try:
            found = fetch_gmail_alerts()
            print(f"  Gmail alerts: {len(found)}", file=sys.stderr)
            all_postings.extend(found)
        except Exception as e:
            w = _source_warning("Gmail", e)
            print(f"  {w}", file=sys.stderr)
            warnings.append(w)

    seen: set[str] = set()
    deduped: list[dict] = []
    for posting in all_postings:
        if posting["id"] not in seen:
            seen.add(posting["id"])
            for field in ("title", "company", "location"):
                if posting.get(field):
                    posting[field] = _html.unescape(posting[field])
            deduped.append(posting)

    JOBS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    JOBS_CACHE.write_text(json.dumps(deduped, indent=2, ensure_ascii=False), encoding="utf-8")

    return deduped, warnings
