from decimal import Decimal

from command_center.orchestrator.planner import _rank_project_candidates


def test_computed_impact_ranks_within_project_wave_and_priority():
    from command_center.orchestrator.wave_plan import Decision

    rows = [
        ("urgent", "1", "P0", "", "", "ai-command-center", True, "functional"),
        ("unbalanced", "1", "P1", "", "", "ai-command-center", True, "functional"),
        ("other-project", "1", "P1", "", "", "aios", True, "functional"),
        ("balanced", "1", "P1", "", "", "~/Projects/ai-command-center", True, "functional"),
        ("next-wave", "2", "P1", "", "", "ai-command-center", True, "functional"),
    ]
    decisions = [
        Decision({"id": "unbalanced"}, "AICC", 1, "Проверить допуск", (), (2, 1, 1)),
        Decision({"id": "balanced"}, "AICC", 1, "Проверить допуск", (), (3, 3, 3)),
    ]
    assert [row[0] for row in _rank_project_candidates(rows, decisions)] == [
        "urgent", "balanced", "other-project", "unbalanced", "next-wave",
    ]


def test_invalid_wave_values_sort_after_numeric_waves():
    from command_center.orchestrator.wave_plan import canonical_wave

    for value in (None, "NaN", "sNaN", "Infinity", "-Infinity", "-1", "1e999", " 1 ", "W1"):
        assert canonical_wave(value) == Decimal("Infinity")
    assert canonical_wave("2") < canonical_wave("10")


def test_wave_recalculates_after_canonical_completion():
    from command_center.orchestrator.planner import repo_route
    from command_center.orchestrator.wave_plan import build_plan

    parent = {
        "id": "parent", "status": "READY_TO_REVIEW", "repo": "aios",
        "kind": "task", "class": "pipeline", "wave": "0", "priority": "P1",
        "acceptance": False,
    }
    child = {**parent, "id": "child", "status": "OPEN", "repo": "ai-command-center"}
    snapshot = {"tasks": [parent, child], "edges": [("child", "parent")], "claims": []}
    before = {item.task["id"]: item for item in build_plan(snapshot, repo_route)}
    assert before["child"].wave == 2
    assert before["child"].blockers == ("parent",)
    completed = {**snapshot, "tasks": [{**parent, "status": "DONE", "acceptance": True}, child]}
    after = {item.task["id"]: item for item in build_plan(completed, repo_route)}
    assert after["child"].blockers == ()
    assert after["child"].wave == 1
    assert after["child"].stage == "Проверить допуск"
    assert after["parent"].stage == "Принято"
