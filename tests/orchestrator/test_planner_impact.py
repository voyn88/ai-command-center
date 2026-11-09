import json

from command_center.orchestrator.planner import _rank_project_candidates


def test_balanced_impact_ranks_within_project_wave_and_priority():
    def candidate(task_id, repo, wave, priority, scores=None):
        fields = ("next_time", "current_quality", "next_cost")
        body = "" if scores is None else "AICC-IMPACT: " + json.dumps({
            field: {"score": score, "check": "Compare against baseline"}
            for field, score in zip(fields, scores, strict=True)
        })
        return task_id, wave, priority, task_id, body, repo, True, "functional"

    rows = [
        candidate("urgent", "ai-command-center", "1", "P0"),
        candidate("unbalanced", "ai-command-center", "1", "P1", (5, 5, 1)),
        candidate("other-project", "aios", "1", "P1"),
        candidate("legacy", "ai-command-center", "1", "P1"),
        candidate("balanced", "~/Projects/ai-command-center", "1", "P1", (3, 3, 3)),
        candidate("next-wave", "ai-command-center", "2", "P1", (5, 5, 5)),
    ]
    assert [row[0] for row in _rank_project_candidates(rows)] == [
        "urgent", "balanced", "other-project", "unbalanced", "legacy", "next-wave",
    ]


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
    parent.update(status="DONE", acceptance=True)
    after = {item.task["id"]: item for item in build_plan(snapshot, repo_route)}
    assert after["child"].wave == 1
    assert after["child"].stage == "Проверить допуск"
    assert after["parent"].stage == "Принято"
