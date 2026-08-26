"""The Markdown backlog parser: exact normalized values, nothing guessed.

Hermetic — the parser is pure. The synthetic fixture reproduces every record
SHAPE observed in the canonical file; the canonical file itself is exercised
only by the local-only test at the bottom, because this repository is public
and the real backlog names internal projects and paths (recorded incident
class: a snapshot must never be committed).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from command_center.db.backlog_parser import parse_backlog

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "backlog_sample.md"

#: The canonical file's location on the orchestration host. Local-only.
REAL_FILE = Path(
    "/Users/dmitrijcernikov/Documents/Codex/2026-08-12/roadmap/outputs/VOYN_TASKS_BACKLOG.md"
)


@pytest.fixture(scope="module")
def report():
    return parse_backlog(FIXTURE.read_text(encoding="utf-8"))


def test_every_shape_parses_to_exact_values(report):
    by_id = {t.task_id: t for t in report.tasks}
    assert by_id["VOYN-W0-S1"].wave == "0"
    assert by_id["VOYN-W0-S1"].priority == "P0"
    assert by_id["VOYN-W0-S1"].status == "OPEN"
    assert by_id["VOYN-W0-S1"].title == "first-task"
    assert "sub-bullets travel into the body" in by_id["VOYN-W0-S1"].body
    assert by_id["VOYN-W0-S1"].repo == "~/somewhere/repo-a"

    # Annotations normalize to exact tokens; the annotation survives as prose.
    annotated = by_id["VOYN-W0-S3"]
    assert annotated.status == "IN_PROGRESS"
    assert annotated.priority == "P0"
    assert "slice 1 DONE" in annotated.body

    assert by_id["VOYN-W0.5-S1"].wave == "0.5", "0.5 is distinct from 0"
    assert by_id["VOYN-COM-S1"].wave == "COM"
    assert by_id["VOYN-POOL-S1"].wave == "W7", "the idea pool, not wave 7"
    assert by_id["VOYN-LANE-P1"].wave == "P1" and by_id["VOYN-LANE-P1"].priority is None
    assert by_id["VOYN-W0-S4"].status == "UNTRIAGED"
    assert by_id["VOYN-W0-S6"].status == "DECIDED"
    assert by_id["VOYN-W0-S5"].priority is None, "older records carry no priority"


def test_gates_are_classified_as_control_records(report):
    kinds = {t.task_id: t.kind for t in report.tasks}
    assert kinds["VOYN-W0-G1"] == "gate"
    assert kinds["VOYN-W0-S1"] == "task"


def test_nothing_is_lost_silently(report):
    reasons = {reason.split(":")[0] for _, reason, _ in report.unparsed}
    assert "status outside vocabulary" in reasons  # VOYN-W0-S2
    assert "wave does not normalize" in reasons  # VOYN-BAD-WAVE
    assert any("duplicate id" in reason for _, reason, _ in report.unparsed)
    assert "id outside the VOYN namespace" in reasons  # NOT-VOYN-1
    # And the reports carry line numbers pointing at the actual lines.
    for line_no, _, excerpt in report.unparsed:
        assert excerpt in FIXTURE.read_text(encoding="utf-8").splitlines()[line_no - 1]


def test_the_first_duplicate_occurrence_wins(report):
    matches = [t for t in report.tasks if t.task_id == "VOYN-W0-S1"]
    assert len(matches) == 1
    assert matches[0].title == "first-task", "the later stray copy must not win"


@pytest.mark.skipif(not REAL_FILE.exists(), reason="canonical backlog not on this host")
def test_the_real_canonical_file_parses_with_no_vocabulary_gaps():
    """Local-only: on the orchestration host the parser must account for
    every record — the only tolerated unparsed lines are the file's own
    duplicate ids (a defect the report exists to surface)."""
    if os.environ.get("CI"):
        pytest.skip("the canonical file never travels to CI")
    report = parse_backlog(REAL_FILE.read_text(encoding="utf-8"))
    assert len(report.tasks) > 300
    # The only tolerated report categories on the canon are the file's own
    # defects: duplicate ids, and bold record-shaped NOTES whose "id" is an
    # id plus prose (e.g. "VOYN-… — итог доставки") — both surfaced, neither
    # parsed into a record by guesswork.
    for _line_no, reason, _excerpt in report.unparsed:
        assert reason.startswith(("duplicate id", "id outside the VOYN namespace")), (
            reason
        )


def test_repo_is_inferred_from_the_task_family() -> None:
    """The whole backlog routes without a per-record hint: family → repo,
    non-code families → None (reported, never mis-routed), explicit hint wins."""
    from command_center.db.backlog_parser import _infer_repo, parse_backlog

    assert _infer_repo("VOYN-W0-PLAT-09") == "aios"
    assert _infer_repo("VOYN-W0-AICC-SRV-08") == "ai-command-center"
    assert _infer_repo("VOYN-W0-F2") == "aios"
    assert _infer_repo("VOYN-W0-F3") == "ai-command-center"
    assert _infer_repo("VOYN-OPS-CI-SPEED-01") is None
    assert _infer_repo("VOYN-W0-BE-ACC") == "ai-command-center"

    md = (
        "- **VOYN-W0-AICC-X** | Wave 0 | OPEN | P0 | T | `s` | body.\n"
        "- **VOYN-W0-PLAT-Y** | Wave 0 | OPEN | P0 | T | `s` | body.\n"
        "  - Target repo (owner decision): `aios`.\n"
    )
    report = parse_backlog(md)
    by_id = {t.task_id: t.repo for t in report.tasks}
    assert by_id["VOYN-W0-AICC-X"] == "ai-command-center"  # inferred
    assert by_id["VOYN-W0-PLAT-Y"] == "aios"  # explicit hint (also matches family)

    # Diverging hint must win over inference (reversing the if-order is a real
    # regression the same-repo case above cannot catch — review found it).
    md2 = (
        "- **VOYN-W0-AICC-Z** | Wave 0 | OPEN | P0 | T | `s` | body.\n"
        "  - Target repo (owner decision): `aios`.\n"
    )
    r2 = parse_backlog(md2)
    assert r2.tasks[0].repo == "aios"  # hint aios beats AICC-family inference
