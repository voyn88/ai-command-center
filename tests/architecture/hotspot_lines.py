"""Hotspot line-count fitness gate (VOYN-W0-AICC-HOTSPOT-EXTRACTION-SEAMS).

A UX/code audit found a small set of files carrying a disproportionate share
of AICC's blast radius (``docs/HOTSPOT_EXTRACTION_SEAMS.md``). The audit
verdict was that a mass rewrite of these files is not wanted; instead, every
change that touches one must extract the touched behavior into a seam module
with a characterization test before changing it. That part is procedural and
not mechanically checkable. What *is* cheap to check is the visible symptom
of skipping the policy: the hotspot file keeps growing.

This module implements that single gate, consumed by
``tests/architecture/test_hotspot_line_fitness.py``: each tracked file's
current line count must not exceed the ceiling frozen in
``tests/architecture/HOTSPOT_LINE_BASELINE.json``. Shrinking is always
allowed. Growing past the ceiling, or a tracked file going missing, fails the
gate.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_FILE = Path(__file__).resolve().parent / "HOTSPOT_LINE_BASELINE.json"


def load_baseline() -> dict[str, int]:
    raw = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    return dict(raw["ceilings"])


def count_lines(rel_path: str, root: Path = REPO_ROOT) -> int:
    path = root / rel_path
    with path.open("r", encoding="utf-8", errors="surrogateescape") as handle:
        return sum(1 for _ in handle)


def compute_line_counts(rel_paths, root: Path = REPO_ROOT) -> dict[str, int]:
    """Line counts for every tracked file that still exists.

    A missing file is omitted here rather than raising, so a rename or
    deletion surfaces as a normal, readable gate failure (``diff_against_baseline``
    reports it as a missing hotspot file) instead of an unrelated traceback.
    """
    counts: dict[str, int] = {}
    for rel_path in rel_paths:
        if (root / rel_path).exists():
            counts[rel_path] = count_lines(rel_path, root)
    return counts


def diff_against_baseline(counts: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Human-readable drift lines; empty list means the gate is green."""
    problems: list[str] = []
    for rel_path, ceiling in sorted(baseline.items()):
        if rel_path not in counts:
            problems.append(
                f"MISSING HOTSPOT FILE: {rel_path} is in the baseline but no "
                "longer exists at that path — if it was retired or renamed, "
                "update tests/architecture/HOTSPOT_LINE_BASELINE.json "
                "(docs/HOTSPOT_EXTRACTION_SEAMS.md)"
            )
            continue
        actual = counts[rel_path]
        if actual > ceiling:
            problems.append(
                f"HOTSPOT GROWTH: {rel_path} is {actual} lines, above its "
                f"frozen ceiling of {ceiling} (docs/HOTSPOT_EXTRACTION_SEAMS.md) "
                "— pull the touched behavior out through an extraction seam "
                "with a characterization test instead of growing this file "
                "further"
            )
    return problems
