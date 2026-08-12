"""
Resume tailor agent.

Takes the base resume (data/profile/resume.txt) and a specific job posting,
and rewrites bullet points and summary to mirror the language and priorities
of that posting, without fabricating experience.

Hard rule: never invent skills, titles, or achievements not present in the
base resume. Tailoring means re-emphasis and re-ordering, not fabrication.
"""

from agents.claude_client import complete, parse_reply_and_draft

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
6. In the Skills and Competencies section, keep every line in its original position.
   Do not reorder items to put the most relevant ones first. If an item should be
   swapped for a more relevant one, replace the text of that line in place rather
   than moving it elsewhere in the list. This keeps the draft easy to diff against
   the original resume.
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


REVISE_PROMPT = """Base resume (ground truth, never invent beyond this):
{resume}

Job posting:
{posting}

Current tailored resume draft:
{draft}

The candidate gave this feedback on the draft:
{feedback}

Revise the draft to address the feedback. Follow these rules exactly:
1. Do not invent any skill, title, or achievement not in the base resume.
2. Do not use hyphens or semicolons anywhere in the output.
3. Only change what the feedback asks for. Preserve everything else in the draft as is.
4. Keep the same general structure (summary, experience, skills) as the original.
5. In the Skills and Competencies section, keep every line in its original position.
   Do not reorder items. If an item should be swapped for a more relevant one,
   replace the text of that line in place rather than moving it elsewhere in the list.

Respond in exactly this format, with no other text:
<reply>
One or two sentence acknowledgment of what you changed.
</reply>
<draft>
The full revised resume text.
</draft>
"""


def revise_resume(posting: dict, resume_text: str, current_draft: str, feedback: str) -> tuple[str, str, dict]:
    """
    Revise an already tailored resume draft based on user feedback.

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
        resume=resume_text,
        posting=posting_block,
        draft=current_draft,
        feedback=feedback,
    )
    result = complete(prompt, system=TAILOR_SYSTEM, max_tokens=3000)
    reply, revised_draft = parse_reply_and_draft(result["text"])
    usage = {
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": result["model"],
    }
    return reply, revised_draft, usage
