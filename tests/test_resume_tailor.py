"""
Tests for agents/resume_tailor — revise_resume. Claude calls are mocked.
"""

from unittest.mock import patch

from agents.resume_tailor import revise_resume


POSTING = {
    "id": "abc123",
    "title": "VP Engineering",
    "company": "Acme",
    "location": "San Francisco, CA",
    "description": "Lead our engineering org.",
}

TAGGED_RESPONSE = {
    "text": "<reply>\nLed with the platform migration bullet.\n</reply>\n<draft>\nRevised resume text.\n</draft>",
    "input_tokens": 900,
    "output_tokens": 200,
    "model": "claude-sonnet-4-6",
}


def test_revise_resume_parses_reply_and_draft():
    with patch("agents.resume_tailor.complete", return_value=TAGGED_RESPONSE):
        reply, draft, usage = revise_resume(POSTING, "Base resume text.", "Original draft.", "Lead with platform migration")
    assert reply == "Led with the platform migration bullet."
    assert draft == "Revised resume text."
    assert usage == {"input_tokens": 900, "output_tokens": 200, "model": "claude-sonnet-4-6"}


def test_revise_resume_falls_back_when_untagged():
    untagged = {**TAGGED_RESPONSE, "text": "Just the plain revised resume with no tags."}
    with patch("agents.resume_tailor.complete", return_value=untagged):
        reply, draft, _ = revise_resume(POSTING, "Base resume text.", "Original draft.", "Feedback")
    assert reply == "Updated the draft."
    assert draft == "Just the plain revised resume with no tags."


def test_revise_resume_passes_feedback_and_current_draft_to_prompt():
    with patch("agents.resume_tailor.complete", return_value=TAGGED_RESPONSE) as mock_complete:
        revise_resume(POSTING, "Base resume text.", "Current draft body.", "Make bullet 2 punchier")
    prompt = mock_complete.call_args[0][0]
    assert "Current draft body." in prompt
    assert "Make bullet 2 punchier" in prompt
    assert "Base resume text." in prompt
