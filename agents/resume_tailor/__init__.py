"""
Resume tailor agent.

Takes the base resume (data/profile/resume.txt) and a specific job posting,
and rewrites bullet points and summary to mirror the language and priorities
of that posting, without fabricating experience.

Hard rule: never invent skills, titles, or achievements not present in the
base resume. Tailoring means re-emphasis and re-ordering, not fabrication.
"""

from agents.claude_client import complete

TAILOR_SYSTEM = (
    "You are a professional resume writer. You rewrite resumes to be highly relevant "
    "to a specific job posting without inventing any experience. Every skill, title, "
    "and achievement in the output must exist in the base resume. Tailoring means "
    "choosing which accomplishments to lead with and mirroring the job's language, "
    "not adding new ones."
)

TAILOR_PROMPT = """Base resume:
{resume}

Job posting:
{posting}

Rewrite the resume summary and bullet points to emphasize the experience most
relevant to this posting. Follow these rules exactly:
1. Do not invent any skill, title, or achievement not in the base resume.
2. Do not use hyphens or semicolons anywhere in the output.
3. Return only the rewritten resume text, no preamble or commentary.
4. Keep the same general structure (summary, experience, skills) as the original.
5. Adjust word choice to mirror terminology from the posting where accurate.
"""


def tailor_resume(posting: dict, resume_text: str) -> tuple[str, dict]:
    """
    Rewrite resume_text for the given posting.

    Returns:
      (tailored_text, usage)  where usage = {input_tokens, output_tokens, model}
    """
    posting_block = "\n".join([
        f"Title: {posting.get('title', '')}",
        f"Company: {posting.get('company', '')}",
        f"Location: {posting.get('location', '')}",
        "",
        posting.get("description", "")[:3000],
    ])
    prompt = TAILOR_PROMPT.format(resume=resume_text, posting=posting_block)
    result = complete(prompt, system=TAILOR_SYSTEM, max_tokens=3000)
    usage = {
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": result["model"],
    }
    return result["text"], usage
