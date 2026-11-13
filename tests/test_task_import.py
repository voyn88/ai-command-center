import json

import pytest

from command_center import models, task_import as ti, tasks_repository


def _task(**overrides) -> dict:
    base = {
        "id": "PKG-001",
        "title": "Do the thing",
        "goal": "Do the thing in detail",
        "prompt": "Do it.",
        "repository_path": "/repo",
        "workspace_path": "/repo",
        "branch": "main",
        "status": "Backlog",
        "project": "AIOS",
        "task_type": "implementation",
        "priority": "Medium",
        "depends_on": [],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# parse_task_package
# --------------------------------------------------------------------------


def test_parse_legacy_list_package_auto_generates_package_id():
    parsed = ti.parse_task_package(json.dumps([_task()]))
    assert parsed.schema_version == "legacy"
    assert parsed.package_id.startswith("legacy-")
    assert parsed.tasks == [_task()]


def test_parse_envelope_package_uses_given_id_and_schema_version():
    payload = {"schema_version": "1", "package_id": "wave-1", "tasks": [_task()]}
    parsed = ti.parse_task_package(json.dumps(payload))
    assert parsed.schema_version == "1"
    assert parsed.package_id == "wave-1"
    assert parsed.tasks == [_task()]


def test_parse_malformed_json_raises():
    with pytest.raises(ti.TaskImportError):
        ti.parse_task_package("not json")


def test_parse_envelope_without_tasks_field_raises():
    with pytest.raises(ti.TaskImportError):
        ti.parse_task_package(json.dumps({"schema_version": "1", "package_id": "x"}))


def test_parse_non_list_non_dict_root_raises():
    with pytest.raises(ti.TaskImportError):
        ti.parse_task_package(json.dumps("just a string"))


def test_parse_accepts_bytes():
    parsed = ti.parse_task_package(json.dumps([_task()]).encode("utf-8"))
    assert parsed.tasks == [_task()]


def test_two_parses_of_identical_content_produce_the_same_hash_and_package_id():
    text = json.dumps([_task()])
    assert ti.parse_task_package(text).package_hash == ti.parse_task_package(text).package_hash
    assert ti.parse_task_package(text).package_id == ti.parse_task_package(text).package_id


# --------------------------------------------------------------------------
# validate_task_package
# --------------------------------------------------------------------------


def test_validate_empty_package_is_an_error():
    parsed = ti.parse_task_package(json.dumps([]))
    validation = ti.validate_task_package(parsed)
    assert validation.items == []
    assert len(validation.errors) == 1


def test_validate_missing_required_field_is_an_error():
    task = _task()
    del task["branch"]
    parsed = ti.parse_task_package(json.dumps([task]))
    validation = ti.validate_task_package(parsed)
    assert validation.items == []
    assert len(validation.errors) == 1
    assert "branch" in validation.errors[0].message


def test_validate_empty_depends_on_list_is_not_missing():
    task = _task(depends_on=[])
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert validation.errors == []
    assert len(validation.items) == 1


def test_validate_requires_title_or_goal():
    task = _task()
    del task["title"]
    del task["goal"]
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert len(validation.errors) == 1


def test_validate_title_alone_is_sufficient_without_goal():
    task = _task()
    del task["goal"]
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert validation.errors == []


def test_validate_rejects_unknown_status():
    task = _task(status="Someday")
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert len(validation.errors) == 1
    assert "Someday" in validation.errors[0].message


def test_validate_rejects_unknown_priority():
    task = _task(priority="Whenever")
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert len(validation.errors) == 1


def test_validate_rejects_unknown_task_type():
    task = _task(task_type="not_a_real_type")
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert len(validation.errors) == 1


def test_validate_rejects_non_list_depends_on():
    task = _task(depends_on="PKG-000")
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert len(validation.errors) == 1


def test_validate_duplicate_ids_within_package_is_an_error():
    parsed = ti.parse_task_package(json.dumps([_task(id="DUP"), _task(id="DUP")]))
    validation = ti.validate_task_package(parsed)
    assert len(validation.errors) == 1
    assert "DUP" in validation.errors[0].message
    # the first occurrence is still accepted
    assert len(validation.items) == 1


def test_validate_non_object_entry_is_an_error():
    parsed = ti.parse_task_package(json.dumps([_task(), "not-an-object"]))
    validation = ti.validate_task_package(parsed)
    assert len(validation.errors) == 1
    assert len(validation.items) == 1


def test_validate_does_not_check_dependency_existence_without_a_store():
    """`validate_task_package` never reads the store, so it cannot tell a
    genuinely-unmet dependency from one already satisfied by a prior import —
    that check happens once, accurately, in `build_import_preview`/
    `apply_task_package` (see the tests below)."""
    task = _task(id="PKG-002", depends_on=["PKG-NONEXISTENT"])
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert validation.errors == []
    assert validation.warnings == []


# --------------------------------------------------------------------------
# Project normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_name,expected",
    [
        ("AIOS", "AIOS"),
        ("AICOS", "AICOS"),
        ("AI Command Center", "AICC"),
        ("AICC", "AICC"),
        ("AIOS Product", "PRODUCT"),
        ("PRODUCT", "PRODUCT"),
        ("Ecosystem", "ECOSYSTEM"),
        ("  aios product  ", "PRODUCT"),
        ("ECOSYSTEM", "ECOSYSTEM"),
    ],
)
def test_validate_normalizes_supported_project_names(raw_name, expected):
    task = _task(project=raw_name)
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert validation.errors == []
    assert validation.items[0]["project"] == expected


def test_validate_rejects_unknown_project_name():
    task = _task(project="Totally Unknown Project")
    validation = ti.validate_task_package(ti.parse_task_package(json.dumps([task])))
    assert len(validation.errors) == 1
    assert "Totally Unknown Project" in validation.errors[0].message


# --------------------------------------------------------------------------
# build_import_preview
# --------------------------------------------------------------------------


def test_preview_classifies_new_vs_duplicate_against_the_store(tmp_path):
    existing = tasks_repository.new_task_record("AIOS", "Existing", "implementation", "Backlog")
    existing["id"] = "PKG-DUP"
    tasks_repository.save_tasks(tmp_path, [existing])

    parsed = ti.parse_task_package(json.dumps([_task(id="PKG-DUP"), _task(id="PKG-NEW")]))
    validation = ti.validate_task_package(parsed)
    preview = ti.build_import_preview(tmp_path, parsed, validation)

    assert preview.duplicate_ids == ["PKG-DUP"]
    assert [item["id"] for item in preview.new_items] == ["PKG-NEW"]
    assert preview.total_tasks == 2
    assert not preview.has_blocking_errors


def test_preview_does_not_write_anything(tmp_path):
    parsed = ti.parse_task_package(json.dumps([_task()]))
    validation = ti.validate_task_package(parsed)
    ti.build_import_preview(tmp_path, parsed, validation)
    assert tasks_repository.load_tasks(tmp_path) == []


def test_preview_reports_hash_and_package_id(tmp_path):
    parsed = ti.parse_task_package(json.dumps([_task()]))
    validation = ti.validate_task_package(parsed)
    preview = ti.build_import_preview(tmp_path, parsed, validation)
    assert preview.package_hash == parsed.package_hash
    assert preview.package_id == parsed.package_id


def test_preview_flags_a_dependency_missing_from_both_package_and_store_as_blocking_error(tmp_path):
    """Founder Review remediation: an unresolved dependency defaults to a
    blocking error, not a warning — a typo'd dependency id must never import
    silently."""
    task = _task(id="PKG-002", depends_on=["PKG-NONEXISTENT"])
    parsed = ti.parse_task_package(json.dumps([task]))
    validation = ti.validate_task_package(parsed)
    preview = ti.build_import_preview(tmp_path, parsed, validation)
    assert preview.has_blocking_errors
    assert preview.warnings == []
    assert "PKG-NONEXISTENT" in preview.errors[0].message


def test_preview_downgrades_unresolved_dependency_to_a_warning_when_opted_in(tmp_path):
    task = _task(id="PKG-002", depends_on=["PKG-NONEXISTENT"])
    parsed = ti.parse_task_package(json.dumps([task]))
    validation = ti.validate_task_package(parsed)
    preview = ti.build_import_preview(tmp_path, parsed, validation, allow_unresolved_dependencies=True)
    assert not preview.has_blocking_errors
    assert len(preview.warnings) == 1
    assert "PKG-NONEXISTENT" in preview.warnings[0].message


def test_preview_does_not_warn_when_dependency_is_satisfied_within_the_package(tmp_path):
    tasks = [_task(id="PKG-001", depends_on=[]), _task(id="PKG-002", depends_on=["PKG-001"])]
    parsed = ti.parse_task_package(json.dumps(tasks))
    validation = ti.validate_task_package(parsed)
    preview = ti.build_import_preview(tmp_path, parsed, validation)
    assert preview.warnings == []


def test_preview_does_not_warn_when_dependency_is_already_satisfied_by_the_store(tmp_path):
    """Regression test: a dependency already imported in an earlier package
    must not be reported as missing just because it isn't part of *this*
    package — the check must be against package ids UNION store ids, not
    package ids alone, and it must not resurface a stale warning computed
    before the store was consulted."""
    already_imported = tasks_repository.new_task_record("AIOS", "Earlier dep", "implementation", "Backlog")
    already_imported["id"] = "PKG-DEP"
    tasks_repository.save_tasks(tmp_path, [already_imported])

    task = _task(id="PKG-002", depends_on=["PKG-DEP"])
    parsed = ti.parse_task_package(json.dumps([task]))
    validation = ti.validate_task_package(parsed)
    preview = ti.build_import_preview(tmp_path, parsed, validation)
    assert preview.warnings == []


# --------------------------------------------------------------------------
# Cross-package dependency ordering (Founder Review remediation, item 3):
# "Разрешены: dependency внутри пакета; dependency в актуальном store.
# Неизвестная dependency блокирует импорт всего пакета." — exercised here via
# two real, sequential `apply_task_package` calls (prerequisite package, then
# dependent package), not just a hand-seeded store row.
# --------------------------------------------------------------------------


def test_cross_package_dependency_blocked_when_prerequisite_not_yet_imported(tmp_path):
    dependent = _task(id="GATE-001", depends_on=["PREREQ-001"])
    parsed = ti.parse_task_package(json.dumps([dependent]))
    validation = ti.validate_task_package(parsed)

    preview = ti.build_import_preview(tmp_path, parsed, validation)
    assert preview.has_blocking_errors
    assert "PREREQ-001" in preview.errors[0].message

    with pytest.raises(ti.TaskImportError):
        ti.apply_task_package(tmp_path, parsed, validation)
    assert tasks_repository.load_tasks(tmp_path) == []


def test_cross_package_dependency_resolved_once_prerequisite_package_is_imported(tmp_path):
    prereq = _task(id="PREREQ-001", depends_on=[])
    prereq_parsed = ti.parse_task_package(json.dumps([prereq]))
    prereq_validation = ti.validate_task_package(prereq_parsed)
    prereq_result = ti.apply_task_package(tmp_path, prereq_parsed, prereq_validation)
    assert prereq_result.imported_ids == ["PREREQ-001"]

    dependent = _task(id="GATE-001", depends_on=["PREREQ-001"])
    dependent_parsed = ti.parse_task_package(json.dumps([dependent]))
    dependent_validation = ti.validate_task_package(dependent_parsed)

    # preview: freshly re-reads the store — sees PREREQ-001 now, no blocking error
    preview = ti.build_import_preview(tmp_path, dependent_parsed, dependent_validation)
    assert not preview.has_blocking_errors

    result = ti.apply_task_package(tmp_path, dependent_parsed, dependent_validation)
    assert result.imported_ids == ["GATE-001"]
    assert result.warnings == []

    stored_ids = {t["id"] for t in tasks_repository.load_tasks(tmp_path)}
    assert stored_ids == {"PREREQ-001", "GATE-001"}


def test_preview_and_apply_both_reflect_a_store_mutation_between_two_preview_calls(tmp_path):
    """Directly proves "preview и apply используют актуальный store": the
    *same* dependent package is previewed twice, with the prerequisite
    package imported in between — the first preview must block, the second
    (against the mutated store) must not, with no caching/staleness in
    either `build_import_preview` call."""
    dependent = _task(id="GATE-002", depends_on=["PREREQ-002"])
    dependent_parsed = ti.parse_task_package(json.dumps([dependent]))
    dependent_validation = ti.validate_task_package(dependent_parsed)

    first_preview = ti.build_import_preview(tmp_path, dependent_parsed, dependent_validation)
    assert first_preview.has_blocking_errors

    prereq = _task(id="PREREQ-002", depends_on=[])
    prereq_parsed = ti.parse_task_package(json.dumps([prereq]))
    prereq_validation = ti.validate_task_package(prereq_parsed)
    ti.apply_task_package(tmp_path, prereq_parsed, prereq_validation)

    second_preview = ti.build_import_preview(tmp_path, dependent_parsed, dependent_validation)
    assert not second_preview.has_blocking_errors

    result = ti.apply_task_package(tmp_path, dependent_parsed, dependent_validation)
    assert result.imported_ids == ["GATE-002"]


def test_preview_includes_invalid_rows_from_errors(tmp_path):
    bad_task = _task(id="BAD", status="Someday")
    parsed = ti.parse_task_package(json.dumps([bad_task]))
    validation = ti.validate_task_package(parsed)
    preview = ti.build_import_preview(tmp_path, parsed, validation)
    assert preview.has_blocking_errors
    assert any(row.outcome == "invalid" and row.id == "BAD" for row in preview.rows)


# --------------------------------------------------------------------------
# apply_task_package
# --------------------------------------------------------------------------


def test_apply_refuses_when_validation_has_errors(tmp_path):
    parsed = ti.parse_task_package(json.dumps([_task(status="Someday")]))
    validation = ti.validate_task_package(parsed)
    with pytest.raises(ti.TaskImportError):
        ti.apply_task_package(tmp_path, parsed, validation)
    assert tasks_repository.load_tasks(tmp_path) == []


def test_apply_writes_new_tasks_with_full_workflow_fields(tmp_path):
    parsed = ti.parse_task_package(json.dumps([_task(id="PKG-001")]))
    validation = ti.validate_task_package(parsed)
    result = ti.apply_task_package(tmp_path, parsed, validation)

    assert result.imported_ids == ["PKG-001"]
    assert result.skipped_duplicate_ids == []

    stored = tasks_repository.load_tasks(tmp_path)
    assert len(stored) == 1
    task = stored[0]
    assert task["id"] == "PKG-001"
    assert task["project"] == "AIOS"
    assert task["goal"] == "Do the thing in detail"
    assert task["prompt"] == "Do it."
    assert task["repository_path"] == "/repo"
    assert task["workspace_path"] == "/repo"
    assert task["branch"] == "main"
    assert task["created_at"]
    assert task["updated_at"]
    assert task["timeline"][0]["type"] == "task_created"
    assert task["current_stage"] == models.EXECUTION_STAGES[0]
    assert task["progress"] == 0


def test_apply_preserves_extra_package_fields_as_metadata(tmp_path):
    task = _task(id="PKG-001", source="Founder Audit v2", target_version="2.0.0", parallel_group="WAVE-1")
    parsed = ti.parse_task_package(json.dumps([task]))
    validation = ti.validate_task_package(parsed)
    ti.apply_task_package(tmp_path, parsed, validation)

    stored = tasks_repository.load_tasks(tmp_path)[0]
    assert stored["metadata"]["source"] == "Founder Audit v2"
    assert stored["metadata"]["target_version"] == "2.0.0"
    assert stored["metadata"]["parallel_group"] == "WAVE-1"
    assert stored["metadata"]["package_id"] == parsed.package_id


def test_apply_writes_all_tasks_to_store(tmp_path, monkeypatch):
    # Pre-seed the store file so `load_tasks`'s own lazy first-time-seed
    # write (unrelated to this import) doesn't count against the assertion.
    tasks_repository.load_tasks(tmp_path)

    calls = []
    original_save = tasks_repository.save_tasks

    def spy(root, tasks):
        calls.append(list(tasks))
        original_save(root, tasks)

    monkeypatch.setattr(tasks_repository, "save_tasks", spy)

    tasks = [_task(id=f"PKG-{i}") for i in range(3)]
    parsed = ti.parse_task_package(json.dumps(tasks))
    validation = ti.validate_task_package(parsed)
    ti.apply_task_package(tmp_path, parsed, validation)

    # The entire JSON package commits once after graph validation under lock.
    assert len(calls) == 1
    # The atomic write contains exactly the 3 imported tasks.
    assert len(calls[-1]) == 3


def test_apply_skips_duplicates_already_in_store(tmp_path):
    parsed = ti.parse_task_package(json.dumps([_task(id="PKG-001")]))
    validation = ti.validate_task_package(parsed)
    ti.apply_task_package(tmp_path, parsed, validation)

    # re-parse/validate against the now-populated store
    validation_again = ti.validate_task_package(parsed)
    result = ti.apply_task_package(tmp_path, parsed, validation_again)

    assert result.imported_ids == []
    assert result.skipped_duplicate_ids == ["PKG-001"]
    assert len(tasks_repository.load_tasks(tmp_path)) == 1


def test_apply_is_idempotent_across_repeated_imports_of_the_same_package(tmp_path):
    tasks = [_task(id=f"PKG-{i}") for i in range(5)]
    parsed = ti.parse_task_package(json.dumps(tasks))

    for _ in range(3):
        validation = ti.validate_task_package(parsed)
        ti.apply_task_package(tmp_path, parsed, validation)

    stored = tasks_repository.load_tasks(tmp_path)
    assert len(stored) == 5
    assert sorted(t["id"] for t in stored) == [f"PKG-{i}" for i in range(5)]


def test_apply_does_not_duplicate_a_task_written_concurrently_between_validate_and_apply(tmp_path):
    """Simulates a race: something else writes the same task id into the store
    after `validate_task_package` ran but before `apply_task_package` does.
    `apply_task_package` must re-read the store itself (never trust a
    caller-held snapshot) and treat that id as a duplicate, not a second
    insert."""
    parsed = ti.parse_task_package(json.dumps([_task(id="PKG-RACE")]))
    validation = ti.validate_task_package(parsed)

    concurrent_task = tasks_repository.new_task_record("AIOS", "Written concurrently", "implementation", "Backlog")
    concurrent_task["id"] = "PKG-RACE"
    tasks_repository.save_tasks(tmp_path, [concurrent_task])

    result = ti.apply_task_package(tmp_path, parsed, validation)

    assert result.imported_ids == []
    assert result.skipped_duplicate_ids == ["PKG-RACE"]
    stored = tasks_repository.load_tasks(tmp_path)
    assert len(stored) == 1


def test_apply_blocks_the_whole_package_on_an_unresolved_dependency_by_default(tmp_path):
    task = _task(id="PKG-001", depends_on=["PKG-GHOST"])
    parsed = ti.parse_task_package(json.dumps([task]))
    validation = ti.validate_task_package(parsed)
    with pytest.raises(ti.TaskImportError, match="зависимост"):
        ti.apply_task_package(tmp_path, parsed, validation)
    assert tasks_repository.load_tasks(tmp_path) == []


def test_apply_allows_unresolved_dependency_when_explicitly_opted_in(tmp_path):
    task = _task(id="PKG-001", depends_on=["PKG-GHOST"])
    parsed = ti.parse_task_package(json.dumps([task]))
    validation = ti.validate_task_package(parsed)
    result = ti.apply_task_package(tmp_path, parsed, validation, allow_unresolved_dependencies=True)
    assert result.imported_ids == ["PKG-001"]
    assert any("PKG-GHOST" in w.message for w in result.warnings)


def test_apply_only_writes_the_new_subset_when_package_mixes_new_and_duplicate(tmp_path):
    parsed = ti.parse_task_package(json.dumps([_task(id="PKG-001")]))
    validation = ti.validate_task_package(parsed)
    ti.apply_task_package(tmp_path, parsed, validation)

    mixed_parsed = ti.parse_task_package(json.dumps([_task(id="PKG-001"), _task(id="PKG-002")]))
    mixed_validation = ti.validate_task_package(mixed_parsed)
    result = ti.apply_task_package(tmp_path, mixed_parsed, mixed_validation)

    assert result.imported_ids == ["PKG-002"]
    assert result.skipped_duplicate_ids == ["PKG-001"]
    assert len(tasks_repository.load_tasks(tmp_path)) == 2
