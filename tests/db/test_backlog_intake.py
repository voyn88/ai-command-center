"""Chat-text backlog intake (VOYN-W0-APP-CONTROL-S6a): the model's proposed
line is only ever trusted through ``parse_backlog`` — hermetic, no model
call, no database, no HTTP."""

from __future__ import annotations

from command_center.db.backlog_intake import build_intake_prompt, draft_from_model_output

_VALID_LINE = (
    "- **VOYN-W0-APP-CONTROL-S9** | Wave 0 | UNTRIAGED | P1 | "
    "`voice-chat-intake` | Let the owner file a task by typing a sentence."
)


def test_prompt_embeds_the_owners_text_verbatim():
    prompt = build_intake_prompt("  add a task to fix the flaky CI job  ")
    assert "add a task to fix the flaky CI job" in prompt
    assert "UNTRIAGED" in prompt  # the closed vocabulary the model must use


def test_a_well_formed_line_drafts_to_a_task():
    draft = draft_from_model_output(_VALID_LINE)
    assert draft.ok
    assert draft.task is not None
    assert draft.task.task_id == "VOYN-W0-APP-CONTROL-S9"
    assert draft.task.wave == "0"
    assert draft.task.priority == "P1"
    assert draft.task.status == "UNTRIAGED"
    assert draft.task.title == "voice-chat-intake"


def test_status_is_forced_to_untriaged_even_if_the_model_wrote_something_else():
    line = _VALID_LINE.replace("UNTRIAGED", "OPEN")
    draft = draft_from_model_output(line)
    assert draft.ok
    assert draft.task.status == "UNTRIAGED"


def test_empty_output_is_refused():
    draft = draft_from_model_output("   \n  ")
    assert not draft.ok
    assert draft.task is None
    assert draft.reason == "empty model output"


def test_chatty_preamble_before_the_line_is_ignored():
    draft = draft_from_model_output(f"Sure, here you go:\n{_VALID_LINE}\n")
    assert draft.ok
    assert draft.task.task_id == "VOYN-W0-APP-CONTROL-S9"


def test_a_line_the_grammar_rejects_is_refused_with_the_parsers_reason():
    draft = draft_from_model_output("- **VOYN-W0-BAD** | not-a-wave | UNTRIAGED | `x` | desc")
    assert not draft.ok
    assert draft.task is None
    assert "wave does not normalize" in draft.reason


def test_more_than_one_task_line_is_refused_rather_than_guessed_at():
    two_lines = f"{_VALID_LINE}\n- **VOYN-W0-APP-CONTROL-S10** | Wave 0 | UNTRIAGED | `x` | y"
    draft = draft_from_model_output(two_lines)
    assert not draft.ok
    assert draft.task is None
    assert "expected exactly one task line, found 2" in draft.reason
