#!/usr/bin/python3
"""Refuse systemd units whose RuntimeDirectory= can collide.

systemd does not refcount RuntimeDirectory= across units: whenever ANY unit
that declared a given runtime directory stops, systemd deletes it -- even if
another still-running unit is relying on the same path. Three worker units
that all declared `RuntimeDirectory=voyn-aicc-worker` learned this the hard
way (VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION): stopping one lane deleted the
credential file every other lane was authenticating with, mid-flight.

This module answers one question soundly: across every unit file (and its
drop-ins) under a directory, could ANY two of them -- or two different
instances of the same template -- ever be made to resolve RuntimeDirectory=
to the identical path? "Could" has to survive specifier substitution
(`%i` and friends): a naive text comparison of `shared/%i` against
`shared/%i` looks safe (the strings differ only by which unit declared them)
right up until someone starts `alpha@3` and `beta@3`, at which point both
resolve to `/run/shared/3`. The comparison in this module is over the SET of
paths each declaration can produce, not over the declaration's literal text,
and it never assumes two different units (or two different declared words of
the same template) share an instance string -- each gets its own,
independent free variable.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence, Union

DEFAULT_DIRECTIVE = "RuntimeDirectory"
_ASSIGNMENT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)=(.*)$")
_TEMPLATE_RE = re.compile(r"@\.[A-Za-z0-9]+$")


# --------------------------------------------------------------------------
# Unit discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitSource:
    """One unit's base file text plus its drop-ins, in systemd's apply order."""

    name: str
    fragments: tuple[str, ...]

    @property
    def is_template(self) -> bool:
        return bool(_TEMPLATE_RE.search(self.name))


def _drop_in_paths(unit_path: Path) -> list[Path]:
    drop_in_dir = unit_path.parent / f"{unit_path.name}.d"
    if not drop_in_dir.is_dir():
        return []
    return sorted(p for p in drop_in_dir.iterdir() if p.is_file() and p.suffix == ".conf")


def discover_units(
    root: Path, suffixes: tuple[str, ...] = (".service", ".socket", ".timer", ".mount")
) -> list[UnitSource]:
    units = []
    for path in sorted(root.iterdir()):
        if not (path.is_file() and path.suffix in suffixes):
            continue
        fragments = [path.read_text(encoding="utf-8")]
        fragments.extend(p.read_text(encoding="utf-8") for p in _drop_in_paths(path))
        units.append(UnitSource(name=path.name, fragments=tuple(fragments)))
    return units


# --------------------------------------------------------------------------
# Directive parsing: every occurrence, quoting/escaping/continuation aware.
# --------------------------------------------------------------------------


def _join_continuations(text: str) -> list[str]:
    """A trailing backslash concatenates a line with the next, per
    systemd.syntax(7); the backslash and newline are simply removed."""
    lines = text.split("\n")
    joined: list[str] = []
    buf = ""
    for line in lines:
        if line.endswith("\\"):
            buf += line[:-1]
        else:
            joined.append(buf + line)
            buf = ""
    if buf:
        joined.append(buf)
    return joined


def _raw_directive_values(fragment_text: str, directive: str) -> Iterator[str]:
    for line in _join_continuations(fragment_text):
        stripped = line.strip()
        if not stripped or stripped[0] in "#;" or stripped.startswith("["):
            continue
        match = _ASSIGNMENT_RE.match(stripped)
        if match and match.group(1) == directive:
            yield match.group(2)


def _split_words(directive: str, raw_value: str) -> list[str]:
    """Word-split one directive's value the way systemd.syntax(7) does:
    whitespace-separated, with '...' / "..." quoting and backslash escapes.
    POSIX shell splitting (shlex) is not byte-identical to systemd's grammar
    in every corner case, but it is a real quoting/escaping parser rather
    than the naive str.split() that let a quoted, space-containing directory
    name silently fall apart into two paths."""
    raw_value = raw_value.strip()
    if not raw_value:
        return []
    try:
        return shlex.split(raw_value, comments=False)
    except ValueError as exc:
        raise ValueError(f"cannot parse {directive}={raw_value!r}: {exc}") from exc


def effective_directory_words(unit: UnitSource, directive: str = DEFAULT_DIRECTIVE) -> list[str]:
    """Every directory word this unit effectively declares for `directive`,
    across its base file and every drop-in, in systemd's apply order.

    Each directive occurrence's words are appended to the running list; an
    empty assignment (`RuntimeDirectory=`) resets it, matching systemd's
    reset-on-empty semantics for this class of list directive. Missing any
    of this -- multiple directives, multiple words per directive, drop-ins,
    or the reset -- was exactly how the first attempt at this check missed a
    real collision (VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION-REM round 1).
    """
    words: list[str] = []
    for fragment in unit.fragments:
        for raw in _raw_directive_values(fragment, directive):
            if raw.strip() == "":
                words = []
                continue
            words.extend(_split_words(directive, raw))
    return words


# --------------------------------------------------------------------------
# Specifier-aware pattern matching
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    text: str


@dataclass(frozen=True)
class Specifier:
    letter: str


Token = Union[Literal, Specifier]


def tokenize(word: str) -> list[Token]:
    """Split a directory word into literal text and %-specifier tokens.
    `%%` is the literal-percent escape; any other `%x` is a specifier."""
    tokens: list[Token] = []
    buf: list[str] = []
    i, n = 0, len(word)
    while i < n:
        char = word[i]
        if char == "%" and i + 1 < n:
            nxt = word[i + 1]
            if nxt == "%":
                buf.append("%")
                i += 2
                continue
            if buf:
                tokens.append(Literal("".join(buf)))
                buf = []
            tokens.append(Specifier(nxt))
            i += 2
            continue
        buf.append(char)
        i += 1
    if buf:
        tokens.append(Literal("".join(buf)))
    return tokens


def _chunks(tokens: Sequence[Token]) -> tuple[list[str], bool]:
    """Literal chunks separated by specifier occurrences (len - 1 specifiers
    total), plus whether any specifier letter repeats within this one
    pattern. A repeat means two occurrences must resolve to the *same*
    string within this pattern -- a backreference constraint the glob-style
    check below does not attempt to solve exactly (see patterns_could_collide)."""
    chunks: list[str] = [""]
    seen: set[str] = set()
    repeats = False
    for token in tokens:
        if isinstance(token, Literal):
            chunks[-1] += token.text
        else:
            if token.letter in seen:
                repeats = True
            seen.add(token.letter)
            chunks.append("")
    return chunks, repeats


def _fixed_matches_glob(fixed: str, chunks: Sequence[str]) -> bool:
    pattern = ".*".join(re.escape(chunk) for chunk in chunks)
    return re.fullmatch(pattern, fixed, re.DOTALL) is not None


def _one_is_prefix_of_other(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def _one_is_suffix_of_other(a: str, b: str) -> bool:
    return a.endswith(b) or b.endswith(a)


def patterns_could_collide(tokens_a: Sequence[Token], tokens_b: Sequence[Token]) -> bool:
    """Is there SOME instantiation of A's specifiers and SOME, independently
    chosen, instantiation of B's specifiers that render the identical path?

    A and B are always treated as governed by disjoint variables: nothing
    here ever assumes two patterns share an instance string. That is the
    only sound assumption once the two renderings can come from two
    different running instances -- of two different templates, or of the
    same template started twice (VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION-REM
    round 3: instance names are independent across units).

    - Neither pattern has a specifier: equal iff the literal words are equal.
    - One pattern is a fixed literal, the other has specifiers: does the
      literal match the other's glob (literal chunks separated by `.*`)?
    - Both patterns have specifiers: the only positions systemd's own
      substitution behaviour cannot make agree "for free" (by choosing a
      long-enough instance string) are the two ends of the path -- so it
      reduces to "is A's leading chunk compatible as a shared prefix with
      B's, and A's trailing chunk compatible as a shared suffix with B's".
      Whatever comes between two specifiers can always be reproduced by
      picking large-enough instance strings, so it never adds a constraint.

    A repeated specifier within one side (e.g. `%i-%i`) needs a real
    backreference (word-equation) solve this function does not implement;
    it fails closed and reports a possible collision rather than silently
    assuming the repeat is harmless -- exactly the shortcut that made the
    previous attempt evaluate path components independently of each other
    and miss that repeated occurrences must agree
    (VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION-REM-REM round 3).
    """
    chunks_a, repeats_a = _chunks(tokens_a)
    chunks_b, repeats_b = _chunks(tokens_b)
    if repeats_a or repeats_b:
        return True

    specifiers_a, specifiers_b = len(chunks_a) - 1, len(chunks_b) - 1
    if specifiers_a == 0 and specifiers_b == 0:
        return chunks_a[0] == chunks_b[0]
    if specifiers_a == 0:
        return _fixed_matches_glob(chunks_a[0], chunks_b)
    if specifiers_b == 0:
        return _fixed_matches_glob(chunks_b[0], chunks_a)
    return _one_is_prefix_of_other(chunks_a[0], chunks_b[0]) and _one_is_suffix_of_other(
        chunks_a[-1], chunks_b[-1]
    )


# --------------------------------------------------------------------------
# Hazard scan
# --------------------------------------------------------------------------


def find_hazards(units: Sequence[UnitSource], directive: str = DEFAULT_DIRECTIVE) -> list[str]:
    hazards: list[str] = []
    parsed = [
        (unit, [(word, tokenize(word)) for word in effective_directory_words(unit, directive)])
        for unit in units
    ]

    for unit, words in parsed:
        if not unit.is_template:
            continue
        # A template word with no specifier is shared, verbatim, by every
        # instance -- the exact shape of the original incident.
        for word, tokens in words:
            if not any(isinstance(token, Specifier) for token in tokens):
                hazards.append(
                    f"{unit.name}: {directive}={word!r} has no instance specifier -- "
                    "every instance of this template shares the identical runtime "
                    "directory"
                )
        # Two different declared words of the same template, resolved by
        # two different (independently chosen) instances of it.
        for i in range(len(words)):
            for j in range(i + 1, len(words)):
                word_i, tokens_i = words[i]
                word_j, tokens_j = words[j]
                if patterns_could_collide(tokens_i, tokens_j):
                    hazards.append(
                        f"{unit.name}: {directive}={word_i!r} can collide with its own "
                        f"{directive}={word_j!r} across two different instances"
                    )

    for a in range(len(parsed)):
        for b in range(a + 1, len(parsed)):
            unit_a, words_a = parsed[a]
            unit_b, words_b = parsed[b]
            for word_a, tokens_a in words_a:
                for word_b, tokens_b in words_b:
                    if patterns_could_collide(tokens_a, tokens_b):
                        hazards.append(
                            f"{unit_a.name} ({directive}={word_a!r}) can collide with "
                            f"{unit_b.name} ({directive}={word_b!r})"
                        )

    return hazards


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        type=Path,
        nargs="?",
        default=Path(__file__).resolve().parents[1] / "deploy" / "systemd",
        help="directory of systemd unit files to scan (default: deploy/systemd)",
    )
    args = parser.parse_args(argv)
    hazards = find_hazards(discover_units(args.directory))
    for hazard in hazards:
        print(hazard, file=sys.stderr)
    return 1 if hazards else 0


if __name__ == "__main__":
    raise SystemExit(main())
