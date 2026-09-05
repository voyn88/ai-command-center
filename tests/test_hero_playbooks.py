from command_center import hero_playbooks


def _done_task(**overrides):
    task = {
        "id": overrides.get("id", "t"),
        "project": "AICC",
        "title": "Implement retry backoff",
        "goal": "Implement retry backoff for the worker queue",
        "status": "Done",
        "task_type": "implementation",
        "agent": "claude",
        "latest_verdict": "APPROVED_FOR_COMMIT",
        "started_at": "2026-01-01T10:00:00",
        "finished_at": "2026-01-01T10:30:00",
    }
    task.update(overrides)
    return task


def _new_task(**overrides):
    task = {
        "id": overrides.get("id", "new"),
        "project": "AICC",
        "title": "New scenario",
        "goal": "",
        "status": "Backlog",
        "task_type": "implementation",
    }
    task.update(overrides)
    return task


def test_build_playbook_catalog_empty_when_no_done_tasks():
    tasks = [_new_task()]
    assert hero_playbooks.build_playbook_catalog(tasks) == []


def test_build_playbook_catalog_drops_combos_below_min_sample_size():
    tasks = [_done_task(id="a")]
    assert hero_playbooks.build_playbook_catalog(tasks) == []


def test_build_playbook_catalog_groups_by_project_task_type_agent():
    tasks = [_done_task(id="a"), _done_task(id="b")]
    catalog = hero_playbooks.build_playbook_catalog(tasks)
    assert len(catalog) == 1
    playbook = catalog[0]
    assert playbook.project == "AICC"
    assert playbook.task_type == "implementation"
    assert playbook.agent == "claude"
    assert playbook.sample_size == 2
    assert playbook.success_rate == 1.0
    assert playbook.avg_duration_seconds == 1800.0


def test_build_playbook_catalog_ranks_higher_success_rate_first():
    winners = [_done_task(id=f"w{i}") for i in range(2)]
    losers = [
        _done_task(id=f"l{i}", agent="codex", latest_verdict="NOT_APPROVED_FOR_COMMIT")
        for i in range(2)
    ]
    catalog = hero_playbooks.build_playbook_catalog(winners + losers)
    assert [p.agent for p in catalog] == ["claude", "codex"]
    assert catalog[0].return_score > catalog[1].return_score


def test_build_playbook_catalog_counts_merged_pr_as_success_without_verdict():
    tasks = [
        _done_task(id="a", latest_verdict=None, pull_request_status="merged"),
        _done_task(id="b", latest_verdict=None, pull_request_status="merged"),
    ]
    catalog = hero_playbooks.build_playbook_catalog(tasks)
    assert catalog[0].success_rate == 1.0


def test_suggest_hero_playbook_returns_none_for_empty_catalog():
    assert hero_playbooks.suggest_hero_playbook(_new_task(), []) is None


def test_suggest_hero_playbook_matches_exact_project_and_task_type():
    history = [_done_task(id="a"), _done_task(id="b")]
    catalog = hero_playbooks.build_playbook_catalog(history)

    suggestion = hero_playbooks.suggest_hero_playbook(_new_task(), catalog)

    assert suggestion is not None
    assert suggestion.match_kind == "exact_context"
    assert suggestion.playbook.agent == "claude"


def test_suggest_hero_playbook_falls_back_to_same_task_type_other_project():
    history = [
        _done_task(id="a", project="AIOS"),
        _done_task(id="b", project="AIOS"),
    ]
    catalog = hero_playbooks.build_playbook_catalog(history)

    suggestion = hero_playbooks.suggest_hero_playbook(_new_task(project="ECOSYSTEM"), catalog)

    assert suggestion is not None
    assert suggestion.match_kind == "same_task_type"
    assert suggestion.playbook.project == "AIOS"


def test_suggest_hero_playbook_falls_back_to_similar_title_when_context_differs():
    history = [
        _done_task(id="a", project="AIOS", task_type="review"),
        _done_task(id="b", project="AIOS", task_type="review"),
    ]
    catalog = hero_playbooks.build_playbook_catalog(history)

    new_task = _new_task(project="ECOSYSTEM", task_type="research", title="Implement retry backoff worker")

    suggestion = hero_playbooks.suggest_hero_playbook(new_task, catalog)

    assert suggestion is not None
    assert suggestion.match_kind == "similar_title"
    assert suggestion.similarity >= hero_playbooks.MIN_TEXT_SIMILARITY


def test_suggest_hero_playbook_returns_none_when_nothing_similar():
    history = [
        _done_task(id="a", project="AIOS", task_type="review", title="Rotate TLS certificates"),
        _done_task(id="b", project="AIOS", task_type="review", title="Rotate TLS certificates"),
    ]
    catalog = hero_playbooks.build_playbook_catalog(history)

    new_task = _new_task(project="ECOSYSTEM", task_type="research", title="Draft quarterly marketing plan")

    assert hero_playbooks.suggest_hero_playbook(new_task, catalog) is None
