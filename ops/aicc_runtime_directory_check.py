#!/usr/bin/python3
"""Detect colliding systemd RuntimeDirectory= declarations before install.

Root cause this guards (VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION): systemd
does not refcount RuntimeDirectory= across units on this fleet's systemd
version -- when ANY unit that names a given runtime directory stops, the
directory is deleted out from under every other unit that still names it,
including the credential file a still-running sibling has open. Two units
collide the instant their *effective* directory paths can ever be equal,
not merely when their raw directive text is identical byte-for-byte.

Getting "effective" right requires three things a naive `line.split()`
compare gets wrong (each one a prior adversarial-review rejection):

1. A single `RuntimeDirectory=` directive may list several
   whitespace-separated paths, and the directive may be repeated (values
   accumulate; an empty assignment resets the list). Every one of those
   paths is a separate, independently deleted directory and must be
   enumerated -- not just the first path on the first occurrence.
2. systemd unit file value splitting understands quoting, backslash
   escapes and backslash-newline line continuation. A value like
   `RuntimeDirectory=voyn-aicc-worker/%i shared` is TWO paths, and the
   second one is a plain, unqualified name that can collide with another
   unit's unrelated `RuntimeDirectory=shared` even though the line also
   contains a `%i` specifier somewhere else.
3. `%i`/`%n`/`%N` specifiers are resolved per-instance at start time, not
   at file-comparison time. A path is not "safe" merely because it
   contains a specifier: two different *templates* (not just two
   instances of the same template) can expand to the identical literal
   path for some shared instance name, e.g. both declaring
   `RuntimeDirectory=shared/%i` collide as soon as anyone runs `a@3` and
   `b@3` side by side. Comparison must reason about what the specifier
   could expand to, not skip any path that merely contains one.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Union

# Sentinel marking "the instantiated unit's %i-derived instance value" in a
# flattened specifier sequence. All occurrences within one raw directory
# value, across %i/%n/%N, refer to the exact same instance string.
_VAR = object()

_TYPE_SUFFIXES = (
    ".service",
    ".socket",
    ".timer",
    ".mount",
    ".target",
    ".path",
    ".device",
)


class UnitSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class _LiteralComponent:
    value: str


@dataclass(frozen=True)
class _VarComponent:
    # value == pre + <instance> + post, for whatever instance string the
    # unit is started with.
    pre: str
    post: str


@dataclass(frozen=True)
class _UnresolvedComponent:
    # More than one specifier landed in a single path segment (e.g.
    # "%i-%i" or "%i-%n"). Real unit files never need this; rather than
    # silently mis-modeling the segment as a single pre/post pair, treat
    # any comparison touching it as an unproven collision. Conservative
    # (may over-flag a pattern nobody would write) beats unsound (silently
    # clearing a credential-deleting collision).
    pass


_Component = Union[_LiteralComponent, _VarComponent, _UnresolvedComponent]


def unit_name_parts(filename: str) -> tuple[str, bool]:
    """Return (prefix, is_template) for a systemd unit filename.

    prefix is the text before "@" for a template (or the whole stem for a
    plain unit) -- i.e. specifier %p. is_template says whether %i/%n/%N
    are defined for this unit at all.
    """
    stem = filename
    for suffix in _TYPE_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if "@" in stem:
        prefix = stem.split("@", 1)[0]
        return prefix, True
    return stem, False


def _join_line_continuations(text: str) -> list[str]:
    """Collapse backslash-newline continuations into logical lines."""
    logical: list[str] = []
    pending: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")
        if line.endswith("\\") and not line.endswith("\\\\"):
            pending.append(line[:-1])
            continue
        pending.append(line)
        logical.append(" ".join(pending))
        pending = []
    if pending:
        logical.append(" ".join(pending))
    return logical


def _directive_values(text: str, section: str, key: str) -> list[str]:
    """All effective values of `key` inside `[section]`, in file order.

    Mirrors systemd's list-directive semantics: repeated assignments
    append, and an assignment with an empty right-hand side resets the
    list to empty (so anything accumulated *before* it in the same
    section is discarded).
    """
    values: list[str] = []
    current_section: str | None = None
    for line in _join_line_continuations(text):
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip()
            continue
        if current_section != section:
            continue
        if "=" not in stripped:
            continue
        directive, _, rhs = stripped.partition("=")
        if directive.strip() != key:
            continue
        rhs = rhs.strip()
        if rhs == "":
            values.clear()
            continue
        try:
            tokens = shlex.split(rhs)
        except ValueError as exc:
            raise UnitSyntaxError(f"unparsable {key}= value {rhs!r}: {exc}") from exc
        values.extend(tokens)
    return values


def _expand_specifiers(raw: str, prefix: str, is_template: bool) -> list[object]:
    """Flatten %i/%n/%N/%p/%% into a list of str chunks and `_VAR` markers."""
    pieces: list[object] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            pieces.append("".join(buf))
            buf.clear()

    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "%" and i + 1 < n:
            spec = raw[i + 1]
            if spec == "%":
                buf.append("%")
                i += 2
                continue
            if spec == "i":
                if not is_template:
                    raise UnitSyntaxError(
                        f"%i specifier used outside a template unit: {raw!r}"
                    )
                flush()
                pieces.append(_VAR)
                i += 2
                continue
            if spec in ("n", "N"):
                if is_template:
                    flush()
                    buf.append(f"{prefix}@")
                    flush()
                    pieces.append(_VAR)
                    if spec == "n":
                        buf.append(".service")
                else:
                    buf.append(prefix if spec == "N" else f"{prefix}.service")
                i += 2
                continue
            if spec == "p":
                buf.append(prefix)
                i += 2
                continue
        buf.append(ch)
        i += 1
    flush()
    return pieces


def _pieces_to_components(pieces: list[object]) -> list[_Component]:
    """Split a flattened specifier sequence on literal '/' into segments."""
    segments: list[list[object]] = [[]]
    for piece in pieces:
        if piece is _VAR:
            segments[-1].append(_VAR)
            continue
        assert isinstance(piece, str)
        parts = piece.split("/")
        segments[-1].append(parts[0])
        for part in parts[1:]:
            segments.append([part])

    components: list[_Component] = []
    for segment in segments:
        literals = [p for p in segment if p is not _VAR]
        var_count = sum(1 for p in segment if p is _VAR)
        if var_count == 0:
            components.append(_LiteralComponent("".join(literals)))
        elif var_count == 1:
            idx = segment.index(_VAR)
            pre = "".join(p for p in segment[:idx] if p is not _VAR)
            post = "".join(p for p in segment[idx + 1 :] if p is not _VAR)
            components.append(_VarComponent(pre, post))
        else:
            components.append(_UnresolvedComponent())
    return components


@dataclass(frozen=True)
class DirectoryPattern:
    """One `RuntimeDirectory=` path, decomposed for cross-unit comparison."""

    raw: str
    unit: str
    components: tuple[_Component, ...]

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"{self.unit}:{self.raw}"


def runtime_directory_patterns(unit_path: Path) -> list[DirectoryPattern]:
    """Every effective RuntimeDirectory= path declared by one unit file."""
    text = unit_path.read_text(encoding="utf-8")
    prefix, is_template = unit_name_parts(unit_path.name)
    raw_values = _directive_values(text, "Service", "RuntimeDirectory")
    patterns = []
    for raw in raw_values:
        pieces = _expand_specifiers(raw, prefix, is_template)
        components = tuple(_pieces_to_components(pieces))
        patterns.append(DirectoryPattern(raw=raw, unit=unit_path.name, components=components))
    return patterns


def _rotation_solution_exists(c1: str, c2: str, c3: str) -> bool:
    """Is there a string t with t + c1 == c2 + t + c3?

    This is the general reduction every var/var comparison below bottoms
    out at. Derivation: peeling the shared literal prefix off two
    `pre + t + post` forms always leaves a residual of exactly this shape
    (one side's pre exhausted). Assuming t starts with c2 re-derives the
    identical residual for the remainder, so any solution decomposes as
    t = c2^m + c2[:k] for some m >= 0 and k in [0, len(c2)); the terminal
    condition for that decomposition, independent of m, is
    c1 == rotate_left(c2, k) + c3 for some such k. c2 == "" is the base
    case (no rotation freedom): the t's cancel outright and c1 must equal
    c3 directly.
    """
    if c2 == "":
        return c1 == c3
    for k in range(len(c2)):
        if c1 == c2[k:] + c2[:k] + c3:
            return True
    return False


def _var_var_compatible(a: _VarComponent, b: _VarComponent) -> bool:
    pre_a, post_a, pre_b, post_b = a.pre, a.post, b.pre, b.post
    if len(pre_a) > len(pre_b):
        pre_a, post_a, pre_b, post_b = pre_b, post_b, pre_a, post_a
    if not pre_b.startswith(pre_a):
        return False
    mid = pre_b[len(pre_a) :]
    return _rotation_solution_exists(post_a, mid, post_b)


def _literal_var_compatible(literal: str, var: _VarComponent) -> bool:
    return (
        len(literal) >= len(var.pre) + len(var.post)
        and literal.startswith(var.pre)
        and literal.endswith(var.post)
    )


def _components_compatible(a: _Component, b: _Component) -> bool:
    if isinstance(a, _UnresolvedComponent) or isinstance(b, _UnresolvedComponent):
        return True
    if isinstance(a, _LiteralComponent) and isinstance(b, _LiteralComponent):
        return a.value == b.value
    if isinstance(a, _LiteralComponent) and isinstance(b, _VarComponent):
        return _literal_var_compatible(a.value, b)
    if isinstance(a, _VarComponent) and isinstance(b, _LiteralComponent):
        return _literal_var_compatible(b.value, a)
    assert isinstance(a, _VarComponent) and isinstance(b, _VarComponent)
    return _var_var_compatible(a, b)


def patterns_could_collide(a: DirectoryPattern, b: DirectoryPattern) -> bool:
    """Could a and b ever name the same on-disk directory?

    A specifier can never introduce or remove a path separator (instance
    names cannot contain '/'), so two patterns can only collide if they
    have the same number of path components; every corresponding pair of
    components must then be simultaneously satisfiable by some shared
    instance-name string.
    """
    if len(a.components) != len(b.components):
        return False
    return all(
        _components_compatible(ca, cb) for ca, cb in zip(a.components, b.components)
    )


@dataclass(frozen=True)
class Collision:
    unit_a: str
    unit_b: str
    pattern_a: str
    pattern_b: str


def find_collisions(unit_paths: Iterable[Path]) -> list[Collision]:
    """All colliding RuntimeDirectory= pairs across a set of unit files."""
    paths = list(unit_paths)
    per_unit = {p: runtime_directory_patterns(p) for p in paths}
    collisions: list[Collision] = []
    for i, path_a in enumerate(paths):
        for path_b in paths[i + 1 :]:
            for pattern_a in per_unit[path_a]:
                for pattern_b in per_unit[path_b]:
                    if patterns_could_collide(pattern_a, pattern_b):
                        collisions.append(
                            Collision(
                                unit_a=path_a.name,
                                unit_b=path_b.name,
                                pattern_a=pattern_a.raw,
                                pattern_b=pattern_b.raw,
                            )
                        )
    return collisions
