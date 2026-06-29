"""
Interview prep agent.

Triggered once a job's tracker status moves to "interviewing". Generates:
1. A short company research brief (what they do, recent news, likely priorities)
2. A list of likely behavioral and technical questions based on the posting
3. Suggested talking points from the candidate's background that map to the role

Note on company_context: passing in fresh web search results here is the highest
leverage use of external data. Without it the model relies on training-time
knowledge, which may be stale for fast-moving companies. The caller can fetch
a snippet from a search API and pass it as company_context; if not provided,
the prompt notes that context may be limited.
"""

from agents.claude_client import complete

PREP_SYSTEM = (
    "You are preparing a candidate for a job interview. You write direct, specific prep "
    "materials that reference actual details from the job posting and the candidate's "
    "background. Never write generic advice. Do not use hyphens or semicolons."
)

PREP_PROMPT = """Job posting:
{posting}

Candidate profile:
{profile}

Recent company context (from web search, may be empty if not provided):
{company_context}

Produce the following sections exactly, with these headers:

COMPANY BRIEF
3 to 4 sentences covering what the company does, its market position, and any
recent developments from the context above. If context is empty, note that
and describe the company from the posting alone.

LIKELY BEHAVIORAL QUESTIONS
Five questions the interviewer will probably ask, drawn from the posting's
language and the company's known priorities.

LIKELY TECHNICAL OR ROLE QUESTIONS
Five questions specific to the skills and responsibilities in this posting.

TALKING POINTS
For each of the ten questions above, one sentence suggesting which part of
the candidate's background to draw from.

Rules:
- Do not use hyphens or semicolons anywhere.
- Be specific to this posting and this candidate, not generic.
- Number each question.
"""


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
    if profile.get("notes"):
        lines.append(f"Background: {profile['notes']}")
    return "\n".join(lines)


def generate_prep_brief(posting: dict, profile: dict, company_context: str = "") -> tuple[str, dict]:
    """
    Generate an interview prep brief for the given posting and profile.

    Args:
      posting:         normalized job posting dict
      profile:         candidate profile dict
      company_context: optional string of recent company news / research

    Returns:
      (prep_brief_text, usage)  where usage = {input_tokens, output_tokens, model}
    """
    posting_block = "\n".join([
        f"Title: {posting.get('title', '')}",
        f"Company: {posting.get('company', '')}",
        f"Location: {posting.get('location', '')}",
        f"URL: {posting.get('url', '')}",
        "",
        posting.get("description", "")[:4000],
    ])
    prompt = PREP_PROMPT.format(
        posting=posting_block,
        profile=_fmt_profile(profile),
        company_context=company_context or "(none provided)",
    )
    result = complete(prompt, system=PREP_SYSTEM, max_tokens=2500)
    usage = {
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": result["model"],
    }
    return result["text"], usage
