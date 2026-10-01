"""Architecture fitness gate for AICC's highest blast-radius hotspot files.

Policy: ``docs/HOTSPOT_EXTRACTION_SEAMS.md``. Mechanics: ``hotspot_lines.py``.
Frozen ceilings: ``tests/architecture/HOTSPOT_LINE_BASELINE.json``.
"""

from __future__ import annotations

from tests.architecture import hotspot_lines


def test_hotspot_files_do_not_grow_past_their_frozen_baseline():
    """Growth beyond the frozen ceiling fails; shrinking never does.

    A hotspot file exceeding its baseline is the mechanical symptom of a
    change that skipped the extraction-seam policy (docs/HOTSPOT_EXTRACTION_SEAMS.md)
    — new logic landed inline in the monolith instead of behind a seam.
    """
    baseline = hotspot_lines.load_baseline()
    counts = hotspot_lines.compute_line_counts(baseline)
    problems = hotspot_lines.diff_against_baseline(counts, baseline)
    assert not problems, (
        "Hotspot files must not grow beyond their frozen baseline "
        "(docs/HOTSPOT_EXTRACTION_SEAMS.md, VOYN-W0-AICC-HOTSPOT-EXTRACTION-SEAMS). "
        "Extract the touched behavior into a module with an explicit interface, "
        "pin its current behavior with a characterization test, and only then "
        "make the change.\n" + "\n".join(problems)
    )


def test_diff_logic_flags_growth_allows_shrink_and_flags_missing_files():
    """The gate itself must not rot: growth caught, shrink allowed, missing files caught."""
    baseline = {"a.py": 10, "b.py": 20}

    unchanged = {"a.py": 10, "b.py": 20}
    assert hotspot_lines.diff_against_baseline(unchanged, baseline) == []

    shrunk = {"a.py": 10, "b.py": 15}
    assert hotspot_lines.diff_against_baseline(shrunk, baseline) == []

    grown = {"a.py": 11, "b.py": 20}
    problems = hotspot_lines.diff_against_baseline(grown, baseline)
    assert len(problems) == 1
    assert "a.py" in problems[0]
    assert "HOTSPOT GROWTH" in problems[0]

    missing = {"a.py": 10}
    problems = hotspot_lines.diff_against_baseline(missing, baseline)
    assert len(problems) == 1
    assert "b.py" in problems[0]
    assert "MISSING HOTSPOT FILE" in problems[0]
