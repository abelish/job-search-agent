"""
Cover letter / "why this company" drafter agent.

Takes a job posting and the candidate profile and drafts a short, specific
cover letter or "why this company" statement. Should reference something
concrete from the posting or company, not generic enthusiasm.
"""

from agents.claude_client import complete, parse_reply_and_draft

COVER_LETTER_SYSTEM = (
    "You are a professional cover letter writer. You write concise, specific cover "
    "letters that reference concrete details from the job posting. You never write "
    "generic enthusiasm or hollow phrases like 'I am excited to apply.' Every claim "
    "about the candidate maps to something real in their background."
)

COVER_LETTER_PROMPT = """Candidate profile:
{profile}

Job posting:
{posting}

Write a cover letter of no more than 250 words. Rules:
1. Reference at least one specific detail from the posting or company.
2. Connect the candidate's most relevant experience directly to the role's needs.
3. Do not use hyphens or semicolons anywhere in the output.
4. Do not use filler phrases like "I am excited to" or "I would love to."
5. Return only the cover letter text, no subject line, no preamble.
"""


def _fmt_profile(profile: dict) -> str:
    lines = []
    if profile.get("name"):
        lines.append(f"Name: {profile['name']}")
    if profile.get("current_title"):
        lines.append(f"Current title: {profile['current_title']}")
    if profile.get("target_titles"):
        lines.append(f"Target titles: {', '.join(profile['target_titles'])}")
    if profile.get("core_skills"):
        lines.append(f"Core skills: {', '.join(profile['core_skills'])}")
    if profile.get("domains"):
        lines.append(f"Domains: {', '.join(profile['domains'])}")
    if profile.get("notes"):
        lines.append(f"Background notes: {profile['notes']}")
    return "\n".join(lines)


def draft_cover_letter(posting: dict, profile: dict) -> tuple[str, dict]:
    """
    Draft a cover letter for the given posting and profile.

    Returns:
      (cover_letter_text, usage)  where usage = {input_tokens, output_tokens, model}
    """
    posting_block = "\n".join([
        f"Title: {posting.get('title', '')}",
        f"Company: {posting.get('company', '')}",
        f"Location: {posting.get('location', '')}",
        "",
        posting.get("description", "")[:3000],
    ])
    prompt = COVER_LETTER_PROMPT.format(
        profile=_fmt_profile(profile),
        posting=posting_block,
    )
    result = complete(prompt, system=COVER_LETTER_SYSTEM, max_tokens=600)
    usage = {
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": result["model"],
    }
    return result["text"], usage


REVISE_PROMPT = """Candidate profile:
{profile}

Job posting:
{posting}

Current cover letter draft:
{draft}

The candidate gave this feedback on the draft:
{feedback}

Revise the draft to address the feedback. Follow these rules exactly:
1. Stay under 250 words.
2. Do not use hyphens or semicolons anywhere in the output.
3. Do not use filler phrases like "I am excited to" or "I would love to."
4. Only change what the feedback asks for. Preserve everything else in the draft as is.

Respond in exactly this format, with no other text:
<reply>
One or two sentence acknowledgment of what you changed.
</reply>
<draft>
The full revised cover letter text.
</draft>
"""


def revise_cover_letter(posting: dict, profile: dict, current_draft: str, feedback: str) -> tuple[str, str, dict]:
    """
    Revise an already drafted cover letter based on user feedback.

    Returns:
      (reply, revised_draft, usage)  where reply is a short conversational
      acknowledgment and usage = {input_tokens, output_tokens, model}
    """
    posting_block = "\n".join([
        f"Title: {posting.get('title', '')}",
        f"Company: {posting.get('company', '')}",
        f"Location: {posting.get('location', '')}",
        "",
        posting.get("description", "")[:3000],
    ])
    prompt = REVISE_PROMPT.format(
        profile=_fmt_profile(profile),
        posting=posting_block,
        draft=current_draft,
        feedback=feedback,
    )
    result = complete(prompt, system=COVER_LETTER_SYSTEM, max_tokens=600)
    reply, revised_draft = parse_reply_and_draft(result["text"])
    usage = {
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": result["model"],
    }
    return reply, revised_draft, usage
