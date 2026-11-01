"""Chat-text intake into the structured backlog (``VOYN-W0-APP-CONTROL-S6a``).

The owner types a request in prose; a model turns it into ONE line shaped
exactly like a row of the canonical Markdown backlog
(:mod:`command_center.db.backlog_parser`); that line is run back through
``parse_backlog`` — the same deterministic grammar the file importer already
enforces — before anything is treated as a task. The model never gets to
assert structure directly: it only proposes text, and the existing
no-substring, closed-vocabulary parser is the sole authority on whether that
text is a task. This module owns exactly that seam (prompt in, ``ParsedTask``
or a refusal out) and nothing else — no database access, no HTTP, no model
client, so it is as hermetically testable as the parser it wraps.

Dictated text (``VOYN-W0-APP-CONTROL-S6b``) reaches this module already
repaired by :mod:`command_center.db.voice_transcript`; all this module adds
for it is one extra paragraph of instruction — a recognizer splits an id into
separate words («VOYN W0 APP CONTROL») and the model must be told to join
them rather than invent a new id.

Status is forced to ``UNTRIAGED`` regardless of what the model wrote: a raw
request from chat is exactly the "raw finding" case
``backlog_triage`` (migration 0008) exists for — it must not be able to walk
itself straight into the executable path by writing a different status word.
"""

from __future__ import annotations

import dataclasses

from command_center.db.backlog_parser import ParsedTask, parse_backlog

__all__ = ["IntakeDraft", "build_intake_prompt", "draft_from_model_output"]

_PROMPT_TEMPLATE = """You are drafting ONE line for the AI Command Center's structured \
delivery backlog from a short request written by the product owner.

The backlog's importer accepts only lines shaped EXACTLY like this closed \
grammar — anything else is refused, not guessed at:

- **VOYN-<WAVE-TOKEN>-<FAMILY>-<SLUG>** | Wave <N> | UNTRIAGED | P<digit> | `<short-slug>` | <one-sentence description>

Rules, all mandatory:
- The task id starts with "VOYN-", is ASCII, and contains no spaces or `|` characters.
- "Wave <N>" is the literal wave number the owner mentioned, or "Wave 0" when none was mentioned.
- The status field is always the literal word UNTRIAGED — never invent another status; a human triages it afterwards.
- The priority is optional; when given, it is exactly one bare token "P0" through "P9" (no brackets), immediately after the status field.
- The slug is a short kebab-case identifier inside backticks, e.g. `voice-chat-intake`.
- The description is one plain sentence: no `|` characters, no line breaks.
- Reply with EXACTLY ONE line matching this shape and nothing else: no preamble, no explanation, no markdown fence.
{dictation_note}
Owner's request:
{text}
"""

_DICTATION_NOTE = """
The request below was DICTATED and machine-transcribed, so it may read as \
speech: id parts can arrive as separate words ("VOYN W0 APP CONTROL" means \
the id VOYN-W0-APP-CONTROL — join them with hyphens, never invent a \
different id), punctuation may be missing, and words may be misheard. Fix \
only the transcription: do not add scope the owner did not ask for.
"""


def build_intake_prompt(text: str, *, dictated: bool = False) -> str:
    """The instruction sent to the model — pure string building, no I/O.

    ``dictated`` adds the transcription caveat for voice intake (S6b); the
    grammar itself is identical either way, because the line is judged by the
    same parser no matter how the owner produced the words.
    """
    return _PROMPT_TEMPLATE.format(
        text=text.strip(), dictation_note=_DICTATION_NOTE if dictated else ""
    )


@dataclasses.dataclass(frozen=True, slots=True)
class IntakeDraft:
    ok: bool
    raw_output: str
    task: ParsedTask | None
    reason: str | None


def draft_from_model_output(raw_output: str) -> IntakeDraft:
    """Validate a model's line against the deterministic backlog grammar.

    Exactly one parsed task and nothing left unparsed is required — anything
    ambiguous (zero lines, more than one, a line the parser could not
    normalize) is refused rather than guessed at, the same discipline the
    Markdown importer holds itself to.
    """
    stripped = raw_output.strip()
    if not stripped:
        return IntakeDraft(
            ok=False, raw_output=stripped, task=None, reason="empty model output"
        )

    report = parse_backlog(stripped)
    if report.unparsed:
        _, reason, _ = report.unparsed[0]
        return IntakeDraft(ok=False, raw_output=stripped, task=None, reason=reason)
    if len(report.tasks) != 1:
        return IntakeDraft(
            ok=False,
            raw_output=stripped,
            task=None,
            reason=f"expected exactly one task line, found {len(report.tasks)}",
        )

    task = report.tasks[0]
    if task.status != "UNTRIAGED":
        task = dataclasses.replace(task, status="UNTRIAGED")
    return IntakeDraft(ok=True, raw_output=stripped, task=task, reason=None)
