"""
Tests for agents/cover_letter — revise_cover_letter. Claude calls are mocked.
"""

from unittest.mock import patch

from agents.cover_letter import revise_cover_letter


POSTING = {
    "id": "abc123",
    "title": "VP Engineering",
    "company": "Acme",
    "location": "San Francisco, CA",
    "description": "Lead our engineering org.",
}

PROFILE = {
    "name": "Test User",
    "current_title": "Director, Engineering",
    "target_titles": ["VP Engineering"],
    "core_skills": ["Python", "AWS"],
    "domains": ["SaaS"],
    "notes": "",
}

TAGGED_RESPONSE = {
    "text": "<reply>\nShortened the second paragraph.\n</reply>\n<draft>\nRevised cover letter text.\n</draft>",
    "input_tokens": 500,
    "output_tokens": 150,
    "model": "claude-sonnet-4-6",
}


def test_revise_cover_letter_parses_reply_and_draft():
    with patch("agents.cover_letter.complete", return_value=TAGGED_RESPONSE):
        reply, draft, usage = revise_cover_letter(POSTING, PROFILE, "Original draft.", "Shorten the second paragraph")
    assert reply == "Shortened the second paragraph."
    assert draft == "Revised cover letter text."
    assert usage == {"input_tokens": 500, "output_tokens": 150, "model": "claude-sonnet-4-6"}


def test_revise_cover_letter_falls_back_when_untagged():
    untagged = {**TAGGED_RESPONSE, "text": "Just the plain revised letter with no tags."}
    with patch("agents.cover_letter.complete", return_value=untagged):
        reply, draft, _ = revise_cover_letter(POSTING, PROFILE, "Original draft.", "Feedback")
    assert reply == "Updated the draft."
    assert draft == "Just the plain revised letter with no tags."


def test_revise_cover_letter_passes_feedback_and_current_draft_to_prompt():
    with patch("agents.cover_letter.complete", return_value=TAGGED_RESPONSE) as mock_complete:
        revise_cover_letter(POSTING, PROFILE, "Current draft body.", "Cut the filler in paragraph one")
    prompt = mock_complete.call_args[0][0]
    assert "Current draft body." in prompt
    assert "Cut the filler in paragraph one" in prompt
