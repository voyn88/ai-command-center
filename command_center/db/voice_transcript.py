"""Repair of a dictated transcript before it reaches the intake model
(``VOYN-W0-APP-CONTROL-S6b``).

Speech recognition is transcribing ordinary Russian (or English) speech; it
has never heard of this backlog's closed vocabulary. Dictating «воин W ноль
APP CONTROL, приоритет ноль, волна ноль» reliably comes back with the
namespace token as a real Russian word (``воин``/``война``/``вояж``), the
priority as words (``пи ноль``, ``приоритет ноль``, and — the nastiest of the
family — Cyrillic ``р0``, which *looks* like ``P0`` and is not), and the wave
as ``волна ноль``. That is the known trap recorded on the task, and it is a
deterministic-substitution problem, not a modelling one: the set of terms is
closed and written down, so a table fixes it exactly, where a second model
pass would only add another way to be creatively wrong.

Three properties this module holds to, in order:

* **Pure and transport-agnostic.** No I/O, no model, no HTTP. It takes the
  text a recognizer produced and returns the text plus what it changed, so it
  is equally the Web Speech API's normalizer today and a server-side Whisper
  path's normalizer later (see ``docs`` note in
  ``command_center/api/backlog_intake_routes.py``) — the transcription
  transport is the *only* thing that decision changes.
* **Nothing silent.** Every substitution is reported as a
  :class:`Correction`, the route returns them, and the UI shows them above
  the draft. A wrong repair must be visible and editable, never a quiet
  rewrite of what the owner said — and the owner still confirms the final
  line by hand before anything is written (``/intake/confirm``).
* **Closed vocabulary only.** Substitutions come from the tables below and
  nothing else: no fuzzy matching, no edit distance, no "looks close enough".
  A term that is not in the table is left exactly as dictated and the model
  (and then the deterministic backlog grammar) deals with it — the same
  discipline ``backlog_parser`` holds for the Markdown file.

Scope note: this normalizes *terms*, not grammar. It never produces a backlog
line; it hands better raw material to
:func:`command_center.db.backlog_intake.build_intake_prompt`, whose output is
still judged solely by ``parse_backlog``.
"""

from __future__ import annotations

import dataclasses
import re

__all__ = [
    "Correction",
    "TranscriptNormalization",
    "normalize_transcript",
    "SPOKEN_DIGITS",
    "TERM_REPAIRS",
]


@dataclasses.dataclass(frozen=True, slots=True)
class Correction:
    """One substitution, as heard and as written."""

    heard: str
    written: str


@dataclasses.dataclass(frozen=True, slots=True)
class TranscriptNormalization:
    text: str
    corrections: tuple[Correction, ...]

    @property
    def changed(self) -> bool:
        return bool(self.corrections)


#: Number words a recognizer writes out instead of a digit, in the two
#: languages the owner dictates in. Only 0-9: waves and priorities are single
#: digits in the grammar, so a bigger table would only widen the blast radius.
SPOKEN_DIGITS: dict[str, str] = {
    "ноль": "0", "нуль": "0", "zero": "0", "oh": "0",
    "один": "1", "одна": "1", "первый": "1", "one": "1",
    "два": "2", "две": "2", "второй": "2", "two": "2",
    "три": "3", "третий": "3", "three": "3",
    "четыре": "4", "четвёртый": "4", "четвертый": "4", "four": "4",
    "пять": "5", "пятый": "5", "five": "5",
    "шесть": "6", "шестой": "6", "six": "6",
    "семь": "7", "седьмой": "7", "seven": "7",
    "восемь": "8", "восьмой": "8", "eight": "8",
    "девять": "9", "девятый": "9", "nine": "9",
}

_DIGIT_WORDS = "|".join(sorted(SPOKEN_DIGITS, key=len, reverse=True))
_NUMBER = rf"(?:[0-9]|{_DIGIT_WORDS})"

#: Ordinal-first wave phrasing: «нулевая волна», «вторую волну».
_ORDINAL_WAVE_STEMS: dict[str, str] = {
    "нулев": "0", "перв": "1", "втор": "2", "треть": "3", "третт": "3",
    "четвёрт": "4", "четверт": "4", "пят": "5", "шест": "6", "седьм": "7",
    "восьм": "8", "девят": "9",
}

#: Domain terms a recognizer turns into ordinary words. Whole-word,
#: case-insensitive. Deliberately narrow: each entry is a token that has no
#: plausible meaning of its own inside a task request for THIS product, so
#: repairing it cannot eat a real word the owner meant. `война` ("war") is in
#: the table for exactly that reason — in a delivery-backlog dictation it is
#: the namespace token, never the noun — and, like every other entry, the
#: repair is shown to the owner before anything is written.
TERM_REPAIRS: tuple[tuple[str, str], ...] = (
    # the VOYN-… namespace: the single most-mangled token in the vocabulary
    (r"во(?:и|й)н[а-яё]*", "VOYN"),
    (r"вояж[а-яё]*", "VOYN"),
    (r"voyne?", "VOYN"),
    # product / platform names, dictated as words or spelled out letter by letter
    (r"а\s*и\s*с[иы]\s*с[иы]|а[ий]\s*с[иы]\s*с[иы]|эй\s*ай\s*с[иы]\s*с[иы]|аиси", "AICC"),
    (r"а[ий]\s*о\s*эс|айос|эй\s*ай\s*о\s*эс", "AIOS"),
    (r"апи|эй\s*п[иы]\s*ай", "API"),
    (r"пи\s*ар|пиар", "PR"),
    (r"ю\s*ай|юай", "UI"),
    (r"пи\s*дабл\s*ю\s*эй|пи\s*в[иы]\s*эй", "PWA"),
    (r"кью\s*эй", "QA"),
    (r"с\s*и\s*ай|си\s*ай", "CI"),
)

_TERM_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?<![0-9A-Za-zА-Яа-яЁё-]){pattern}(?![0-9A-Za-zА-Яа-яЁё-])", re.I), canonical)
    for pattern, canonical in TERM_REPAIRS
)

# «волна ноль» / «wave two» / «вейв 3» -> "Wave 0"
_WAVE_PHRASE = re.compile(
    rf"(?<![0-9A-Za-zА-Яа-яЁё-])(волн[а-яё]*|вейв[а-яё]*|wave)\s*[-–—]?\s*({_NUMBER})"
    r"(?![0-9A-Za-zА-Яа-яЁё-])",
    re.I,
)
_ORDINAL_WAVE_PHRASE = re.compile(
    r"(?<![0-9A-Za-zА-Яа-яЁё-])([а-яё]+)\s+(волн[а-яё]*)(?![0-9A-Za-zА-Яа-яЁё-])",
    re.I,
)
# «приоритет ноль» / «пи ноль» / «п 1» / Cyrillic «р0» -> "P0".
# The bare-letter forms carry a Cyrillic `р`/`П` on purpose: a recognizer
# writing the Cyrillic homoglyph produces a token that renders identically to
# the ASCII one and is refused by the parser's ASCII-only priority field.
_PRIORITY_PHRASE = re.compile(
    rf"(?<![0-9A-Za-zА-Яа-яЁё-])(?:приоритет[а-яё]*\s+(?:[pрп]|пи|пэ|пе)?|priority\s+|(?:[pрп]|пи|пэ|пе))"
    rf"\s*[-–—]?\s*({_NUMBER})(?![0-9A-Za-zА-Яа-яЁё-])",
    re.I,
)

_WHITESPACE = re.compile(r"[ \t ]+")


def _digit(token: str) -> str:
    return SPOKEN_DIGITS.get(token.casefold(), token)


def normalize_transcript(text: str) -> TranscriptNormalization:
    """Repair dictated domain terms in ``text``.

    Returns the repaired text and every substitution made, in the order they
    first occur. Text with nothing to repair comes back with its whitespace
    tidied and an empty correction list — the caller can treat "no
    corrections" as "heard cleanly" without comparing strings.
    """
    corrections: list[Correction] = []
    seen: set[tuple[str, str]] = set()

    def record(heard: str, written: str) -> str:
        if heard != written:
            key = (heard, written)
            if key not in seen:
                seen.add(key)
                corrections.append(Correction(heard=heard, written=written))
        return written

    def wave(match: re.Match[str]) -> str:
        return record(match.group(0), f"Wave {_digit(match.group(2))}")

    def ordinal_wave(match: re.Match[str]) -> str:
        stem = match.group(1).casefold()
        for prefix, digit in _ORDINAL_WAVE_STEMS.items():
            if stem.startswith(prefix):
                return record(match.group(0), f"Wave {digit}")
        return match.group(0)

    def priority(match: re.Match[str]) -> str:
        return record(match.group(0), f"P{_digit(match.group(1))}")

    def term(canonical: str):
        def replace(match: re.Match[str]) -> str:
            return record(match.group(0), canonical)

        return replace

    result = _ORDINAL_WAVE_PHRASE.sub(ordinal_wave, text.strip())
    result = _WAVE_PHRASE.sub(wave, result)
    result = _PRIORITY_PHRASE.sub(priority, result)
    for pattern, canonical in _TERM_PATTERNS:
        result = pattern.sub(term(canonical), result)

    return TranscriptNormalization(
        text=_WHITESPACE.sub(" ", result).strip(), corrections=tuple(corrections)
    )
