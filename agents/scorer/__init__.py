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

Location note: The candidate strongly prefers to stay local in the San Francisco Bay Area.
Score on-site or hybrid Bay Area roles 5 to 10 points higher than equivalent roles elsewhere,
all else being equal.

Compensation note: Before concluding that compensation is unlisted, read the full job description
carefully for any mention of salary range, base pay, total compensation, equity, OTE, or annual
pay. Compensation figures often appear at the end of the description after the requirements
section. If a range or figure is present anywhere in the description, cite it in your rationale
and factor it against the candidate's stated minimum compensation.

Bay Area compensation benchmarks are significantly higher than most other markets. For roles that
are manager-level (Engineering Manager, Senior Engineering Manager) rather than director-level or
above (Director, VP, Head of, CTO), and located outside the Bay Area, score 10 to 15 points lower
than you otherwise would. These roles rarely offer the compensation range the candidate requires
without a relocation premium that most companies will not provide.
"""

_BAY_AREA_TERMS = {
    "san francisco", "bay area", "sf, ca", "san jose", "oakland", "berkeley",
    "silicon valley", "south bay", "east bay", "palo alto", "mountain view",
    "santa clara", "sunnyvale", "cupertino", "redwood city", "san mateo",
    "menlo park", "fremont", "hayward", "pleasanton", "walnut creek",
}

# Titles at director level or above — roles below this threshold carry lower
# comp ranges outside the Bay Area and get a penalty score adjustment.
_DIRECTOR_PLUS_TERMS = {
    "director", "vp", "vice president", "head of", "cto", "chief",
}


def _is_director_plus(title: str) -> bool:
    t = title.lower()
    return any(term in t for term in _DIRECTOR_PLUS_TERMS)


def passes_hard_filters(posting: dict, profile: dict) -> bool:
    """
    Deterministic pre-filter against hard profile constraints.
    Returns False on any hard fail; saves Claude calls for clear misses.

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
        return False

    title_lower = posting.get("title", "").lower()
    for kw in profile.get("exclude_title_keywords", []):
        if kw.lower() in title_lower:
            return False

    must_have = profile.get("must_have_keywords", [])
    if must_have:
        searchable = (posting.get("title", "") + " " + posting.get("description", "")).lower()
        if not all(kw.lower() in searchable for kw in must_have):
            return False

    # Title keyword filter: at least ONE must appear in the job title (OR logic).
    # Use this to restrict to specific role levels or functions without touching description.
    must_have_title = profile.get("must_have_title_keywords", [])
    if must_have_title:
        title = posting.get("title", "").lower()
        if not any(kw.lower() in title for kw in must_have_title):
            return False

    location = posting.get("location", "").lower()
    if location:
        locs = profile.get("locations", {})
        is_remote = "remote" in location
        if is_remote and not locs.get("remote_ok", True):
            return False
        if not is_remote and not locs.get("willing_to_relocate", False):
            preferred = [c.lower() for c in locs.get("city_allowlist_international", [])]
            if preferred and not any(c in location for c in preferred):
                return False
        # German sources always enforce city_allowlist_international regardless of willing_to_relocate,
        # since the user may be open to relocating globally but still only want Berlin in Germany.
        if not is_remote and posting.get("source") in {"arbeitsagentur", "smartrecruiters"}:
            preferred = [c.lower() for c in locs.get("city_allowlist_international", [])]
            if preferred and not any(c in location for c in preferred):
                return False

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
            return False
        # For German-source postings in English: reject if German language is explicitly required.
        if posting.get("source") in {"arbeitsagentur", "smartrecruiters"}:
            full_text = (posting.get("title", "") + " " + posting.get("description", "")).lower()
            german_required_markers = [
                "german required", "german language required", "german fluency",
                "german proficiency", "must speak german", "german speaking required",
                "deutsch erforderlich", "deutschkenntnisse erforderlich", "fließend deutsch",
            ]
            if any(m in full_text for m in german_required_markers):
                return False

    return True


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

    location_lower = posting.get("location", "").lower()
    is_bay_area = any(term in location_lower for term in _BAY_AREA_TERMS)
    if is_bay_area:
        boost = 8
        score = min(100, score + boost)
        rationale = rationale + f" (Bay Area location boost: +{boost})"
    elif not _is_director_plus(posting.get("title", "")):
        penalty = 10
        score = max(0, score - penalty)
        rationale = rationale + f" (Non-Bay-Area manager-level penalty: -{penalty})"

    return {
        **posting,
        "fit_score": score,
        "fit_rationale": rationale,
        "_input_tokens": result["input_tokens"],
        "_output_tokens": result["output_tokens"],
        "_model": result["model"],
    }


def _strip_adjustment(score: int, rationale: str) -> tuple[int, str]:
    """
    Reverse any previously applied score adjustment suffix, returning
    (raw_claude_score, clean_rationale) so adjustments can be re-applied fresh.
    """
    m = re.search(r' \(Bay Area location boost: \+(\d+)\)$', rationale)
    if m:
        return score - int(m.group(1)), rationale[:m.start()]
    m = re.search(r' \(Non-Bay-Area manager-level penalty: -(\d+)\)$', rationale)
    if m:
        return score + int(m.group(1)), rationale[:m.start()]
    return score, rationale


def reapply_adjustments(jobs: list[dict]) -> list[dict]:
    """
    Re-apply location/seniority adjustments to already-scored jobs without
    calling Claude. Strips any existing adjustment suffix, recovers the raw
    Claude score, then applies current boost/penalty logic.

    Returns only jobs whose score or rationale actually changed.
    """
    updated = []
    for job in jobs:
        score = job.get("fit_score")
        rationale = job.get("fit_rationale") or ""
        if score is None or not rationale:
            continue

        raw_score, clean_rationale = _strip_adjustment(score, rationale)

        location_lower = job.get("location", "").lower()
        is_bay_area = any(term in location_lower for term in _BAY_AREA_TERMS)

        if is_bay_area:
            boost = 8
            new_score = min(100, raw_score + boost)
            new_rationale = clean_rationale + f" (Bay Area location boost: +{boost})"
        elif not _is_director_plus(job.get("title", "")):
            penalty = 10
            new_score = max(0, raw_score - penalty)
            new_rationale = clean_rationale + f" (Non-Bay-Area manager-level penalty: -{penalty})"
        else:
            new_score = raw_score
            new_rationale = clean_rationale

        if new_score != score or new_rationale != rationale:
            updated.append({**job, "fit_score": new_score, "fit_rationale": new_rationale})

    return updated


def score_all(postings: list[dict], profile: dict, threshold: int = 70, on_progress: callable = None, should_stop: callable = None) -> tuple[list[dict], list[dict]]:
    """
    Run passes_hard_filters then score_posting on each posting.
    Returns (above_threshold, below_threshold), each sorted by score descending.
    Both lists carry the real fit_score and fit_rationale from Claude.
    Logs a score_run activity entry with total token usage.
    """
    above: list[dict] = []
    below: list[dict] = []
    total_input = 0
    total_output = 0
    model = DEFAULT_MODEL
    skipped_hard = 0

    for posting in postings:
        if should_stop and should_stop():
            break
        if not passes_hard_filters(posting, profile):
            skipped_hard += 1
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
        "skipped_hard_filter": skipped_hard,
        "above_threshold": len(above),
        "threshold": threshold,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "model": model,
    })

    return above, below
