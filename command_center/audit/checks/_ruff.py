"""Shared helper: run ruff over a target tree and parse its JSON output.

Three checks (lint, security, code-quality) reuse ruff — the linter this repo
already depends on — as their signal source, each selecting a different rule
family. Centralising the invocation here keeps the tool-plumbing (executable
resolution, JSON parsing, robustness) in one audited place, so each check is just
"pick a rule set, map codes to severity".

Robustness: ruff is invoked as ``python -m ruff`` (always matches the installed
package, no PATH guess). Any failure to launch or parse — ruff absent, a non-JSON
payload, a crash — yields an empty diagnostic list rather than an exception, so a
missing tool degrades the pass to "no findings from this check" instead of
failing it (see :class:`command_center.audit.checks.base.Check`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Bounded so a pathological tree cannot make one check dominate a pass; ruff
# itself is fast, this only guards against a runaway target.
_RUFF_TIMEOUT_SECONDS = 120


def run_ruff(target: Path, *, select: list[str], extra_args: list[str] | None = None) -> list[dict[str, Any]]:
    """Run ``ruff check --select <select> --output-format json`` over ``target``
    and return the parsed diagnostics (a list of dicts). Returns ``[]`` on any
    failure to run or parse — never raises.

    ``select`` is the rule family to enable (e.g. ``["S"]`` for the flake8-bandit
    security rules); ``extra_args`` appends further ruff flags."""
    cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--select",
        ",".join(select),
        "--output-format",
        "json",
        "--no-cache",
        *(extra_args or []),
        str(target),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_RUFF_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # ruff could not be launched (absent interpreter module, timeout, ...).
        return []
    return parse_ruff_json(proc.stdout)


def parse_ruff_json(stdout: str) -> list[dict[str, Any]]:
    """Parse ruff's ``--output-format json`` payload into a list of diagnostic
    dicts. Returns ``[]`` for empty or malformed output (best-effort)."""
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def relative_file(diagnostic: dict[str, Any], target: Path) -> str | None:
    """The diagnostic's filename made relative to ``target`` when possible, so a
    stored finding never leaks an absolute machine-local path (privacy)."""
    filename = diagnostic.get("filename")
    if not filename:
        return None
    try:
        return str(Path(filename).resolve().relative_to(target.resolve()))
    except (ValueError, OSError):
        # Not under target (or unresolvable): fall back to the basename only,
        # never the absolute path.
        return Path(filename).name


def location(diagnostic: dict[str, Any]) -> str | None:
    """A compact ``row:col`` location string, or ``None`` when ruff gave none."""
    loc = diagnostic.get("location") or {}
    row = loc.get("row")
    col = loc.get("column")
    if row is None:
        return None
    return f"{row}:{col}" if col is not None else str(row)
