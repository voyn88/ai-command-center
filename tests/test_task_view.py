from command_center import models, task_view


def test_kanban_status_options_keep_canonical_order():
    assert task_view.kanban_status_options("Review") == [
        status for status in models.KANBAN_STATUSES if status != "Done"
    ]


def test_kanban_status_options_preserves_done_without_offering_new_done():
    options = task_view.kanban_status_options("Done")
    assert options[0] == "Done"
    assert options.count("Done") == 1


def test_kanban_status_options_preserve_legacy_status_without_silent_rewrite():
    options = task_view.kanban_status_options("Blocked")
    assert options[0] == "Blocked"
    assert options[1:] == [status for status in models.KANBAN_STATUSES if status != "Done"]


def test_cached_git_status_returns_not_repo_for_missing_path():
    assert task_view.cached_git_status(None, {}) == {"is_repo": False}


def test_cached_git_status_memoizes_per_path(tmp_path, monkeypatch):
    calls = []

    def fake_get_status(path):
        calls.append(path)
        return {"is_repo": True, "dirty": False}

    monkeypatch.setattr(task_view.git_info, "get_status", fake_get_status)
    cache: dict[str, dict] = {}
    task_view.cached_git_status(str(tmp_path), cache)
    task_view.cached_git_status(str(tmp_path), cache)
    assert len(calls) == 1


def test_cached_git_status_separate_paths_not_shared(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(task_view.git_info, "get_status", lambda path: calls.append(path) or {"is_repo": True})
    cache: dict[str, dict] = {}
    task_view.cached_git_status(str(tmp_path / "a"), cache)
    task_view.cached_git_status(str(tmp_path / "b"), cache)
    assert len(calls) == 2


def test_set_manual_launch_status_updates_task_and_timeline():
    task = {}
    models.normalize_task_execution(task)
    task_view.set_manual_launch_status(task, "Requires Attention", "paused manually")
    assert task["launch_status"] == "Requires Attention"
    assert task["timeline"][-1]["message"] == "paused manually"
    assert task["updated_at"]


def test_sorted_timeline_orders_newest_first():
    task = {
        "timeline": [
            {"ts": "2026-01-01T00:00:00", "type": "a"},
            {"ts": "2026-01-03T00:00:00", "type": "c"},
            {"ts": "2026-01-02T00:00:00", "type": "b"},
        ]
    }
    ordered = task_view.sorted_timeline(task)
    assert [event["type"] for event in ordered] == ["c", "b", "a"]


def test_sorted_timeline_handles_empty_task():
    assert task_view.sorted_timeline({}) == []


def test_dependency_graph_dot_returns_none_when_no_relations():
    task = {"id": "a"}
    assert task_view.dependency_graph_dot(task, {"a": task}) is None


def test_dependency_graph_dot_includes_dependency_edge():
    parent = {"id": "a", "title": "Parent task"}
    child = {"id": "b", "title": "Child task", "depends_on": ["a"]}
    dot = task_view.dependency_graph_dot(child, {"a": parent, "b": child})
    assert dot is not None
    assert '"a" -> "b"' in dot


def test_dependency_graph_dot_includes_parent_child_edge():
    parent = {"id": "p", "title": "Parent"}
    child = {"id": "c", "title": "Child", "parent_task_id": "p"}
    dot = task_view.dependency_graph_dot(parent, {"p": parent, "c": child})
    assert dot is not None
    assert '"p" -> "c"' in dot


def test_dependency_narrative_returns_empty_when_no_relations():
    task = {"id": "a"}
    assert task_view.dependency_narrative(task, {"a": task}) == []


def test_dependency_narrative_names_unmet_dependency_as_a_wait():
    parent = {"id": "a", "title": "Ship the API", "status": "Doing"}
    child = {"id": "b", "title": "Write docs", "depends_on": ["a"]}
    lines = task_view.dependency_narrative(child, {"a": parent, "b": child})
    assert lines == ["Эта задача ждёт «Ship the API»: пока та не будет готова, эта не начнётся."]


def test_dependency_narrative_names_met_dependency_as_cleared():
    parent = {"id": "a", "title": "Ship the API", "status": "Done"}
    child = {"id": "b", "title": "Write docs", "depends_on": ["a"]}
    lines = task_view.dependency_narrative(child, {"a": parent, "b": child})
    assert lines == ["«Ship the API» уже готово — эта задача может продолжаться."]


def test_dependency_narrative_names_blocked_task_as_downstream_impact():
    task = {"id": "a", "title": "Ship the API"}
    blocked = {"id": "b", "title": "Write docs", "depends_on": ["a"]}
    lines = task_view.dependency_narrative(task, {"a": task, "b": blocked})
    assert lines == ["Пока эта задача не будет готова, не сможет начаться «Write docs»."]


def test_dependency_narrative_names_parent_and_children():
    parent = {"id": "p", "title": "Parent goal"}
    child = {"id": "c", "title": "Sub-step", "parent_task_id": "p"}
    parent_lines = task_view.dependency_narrative(parent, {"p": parent, "c": child})
    assert parent_lines == [
        "У этой задачи есть подзадача «Sub-step» — её ход тоже влияет на общий результат."
    ]
    child_lines = task_view.dependency_narrative(child, {"p": parent, "c": child})
    assert child_lines == [
        "Это часть более крупной задачи «Parent goal» — от этого шага зависит, когда та будет готова."
    ]


def test_dependency_narrative_labels_deleted_dependency_by_id():
    task = {"id": "a", "title": "Write docs", "depends_on": ["missing"]}
    lines = task_view.dependency_narrative(task, {"a": task})
    assert lines == ["Эта задача ждёт «(задача missing удалена)»: пока та не будет готова, эта не начнётся."]


# --- Kanban priority filter regression tests -------------------------------
# Regression guard for AICC-CI-001: a task whose priority is outside the
# canonical `models.TASK_PRIORITIES` (here `"P0"`) used to be silently dropped
# from every Kanban lane because the filter only offered/matched the canonical
# priorities. `kanban_priority_options` must surface it and `filter_kanban_tasks`
# must keep it when that option is selected.


def test_kanban_priority_options_include_noncanonical_priority():
    tasks = [
        {"id": "a", "priority": "Critical"},
        {"id": "b", "priority": "P0"},
    ]
    options = task_view.kanban_priority_options(tasks)
    # Canonical priorities come first, in canonical order...
    assert options[: len(models.TASK_PRIORITIES)] == models.TASK_PRIORITIES
    # ...and the non-canonical value is appended so it is selectable.
    assert "P0" in options


def test_kanban_priority_options_no_duplicates():
    tasks = [{"id": "a", "priority": "P0"}, {"id": "b", "priority": "P0"}]
    options = task_view.kanban_priority_options(tasks)
    assert options.count("P0") == 1
    # Canonical values are never duplicated either.
    assert options.count("Medium") == 1


def test_kanban_priority_options_default_missing_priority_to_medium():
    options = task_view.kanban_priority_options([{"id": "a"}])
    assert options == list(models.TASK_PRIORITIES)


def test_filter_kanban_tasks_keeps_task_with_noncanonical_priority_when_selected():
    # This is the AICC-CI-001 scenario: default filter = every offered option.
    tasks = [
        {"id": "AICC-CI-001", "project": "AI Command Center", "priority": "P0", "status": "Backlog"},
        {"id": "other", "project": "AI Command Center", "priority": "Critical", "status": "Backlog"},
    ]
    options = task_view.kanban_priority_options(tasks)
    visible = task_view.filter_kanban_tasks(tasks, project=None, priorities=options)
    assert {t["id"] for t in visible} == {"AICC-CI-001", "other"}


def test_filter_kanban_tasks_excludes_deselected_priority():
    tasks = [
        {"id": "AICC-CI-001", "project": "AI Command Center", "priority": "P0", "status": "Backlog"},
        {"id": "other", "project": "AI Command Center", "priority": "Critical", "status": "Backlog"},
    ]
    # User deselects "P0" — it should disappear; canonical one stays.
    visible = task_view.filter_kanban_tasks(tasks, project=None, priorities=["Critical"])
    assert {t["id"] for t in visible} == {"other"}


def test_filter_kanban_tasks_respects_project_filter():
    tasks = [
        {"id": "a", "project": "AI Command Center", "priority": "P0", "status": "Backlog"},
        {"id": "b", "project": "Other", "priority": "P0", "status": "Backlog"},
    ]
    options = task_view.kanban_priority_options(tasks)
    visible = task_view.filter_kanban_tasks(tasks, project="AI Command Center", priorities=options)
    assert {t["id"] for t in visible} == {"a"}


# --- Kanban project filter regression tests --------------------------------
# Regression guard for AICC-UI-001: a task whose `project` field holds a
# display name / alias ("AI Command Center") instead of the canonical
# `models.PROJECT_IDS` id ("AICC") used to vanish the moment a project was
# selected in the Kanban selector (which emits canonical ids). It rendered on
# the "all projects" board but nowhere under its own project lane.


def test_filter_kanban_tasks_matches_display_name_project_against_canonical_id():
    # The real pipeline: task stores the display name, the selector emits the id.
    tasks = [
        {"id": "AICC-CI-001", "project": "AI Command Center", "priority": "P0", "status": "Backlog"},
        {"id": "other", "project": "AIOS", "priority": "P0", "status": "Backlog"},
    ]
    options = task_view.kanban_priority_options(tasks)
    # Selecting the canonical "AICC" pill must still render AICC-CI-001.
    visible = task_view.filter_kanban_tasks(tasks, project="AICC", priorities=options)
    assert {t["id"] for t in visible} == {"AICC-CI-001"}


def test_filter_kanban_tasks_canonical_id_project_field_still_matches():
    # A task already storing the canonical id keeps matching the id selection.
    tasks = [{"id": "x", "project": "AICC", "priority": "High", "status": "Backlog"}]
    options = task_view.kanban_priority_options(tasks)
    visible = task_view.filter_kanban_tasks(tasks, project="AICC", priorities=options)
    assert {t["id"] for t in visible} == {"x"}


def test_filter_kanban_tasks_unresolvable_project_only_shows_under_all():
    # A project that resolves to no canonical id must not match a real lane,
    # but must still appear on the "all projects" (project=None) board.
    tasks = [{"id": "y", "project": "Totally Unknown", "priority": "High", "status": "Backlog"}]
    options = task_view.kanban_priority_options(tasks)
    assert task_view.filter_kanban_tasks(tasks, project="AICC", priorities=options) == []
    assert {t["id"] for t in task_view.filter_kanban_tasks(tasks, project=None, priorities=options)} == {"y"}
