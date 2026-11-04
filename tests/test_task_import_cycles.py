"""Cycle admission on the local import store, including concurrent packages."""
import json
import threading

import pytest

from command_center import task_import as ti, tasks_repository as tr


def task(task_id, dependencies=(), **extra):
    return dict(id=task_id, title=task_id, repository_path='/repo', workspace_path='/repo',
                branch='main', status='Backlog', project=extra.get('project', 'AIOS'),
                task_type='implementation', priority='Medium', depends_on=list(dependencies))


def package(tasks):
    parsed = ti.parse_task_package(json.dumps(tasks), filename='tasks.json')
    validation = ti.validate_task_package(parsed)
    assert not validation.errors
    return parsed, validation


@pytest.mark.parametrize('tasks', [[task('A', ['A'])], [task('A', ['B']), task('B', ['A'])]])
@pytest.mark.parametrize('allow_unresolved', [False, True])
def test_cycle_is_blocking_in_preview_and_apply_without_any_write(tmp_path, tasks, allow_unresolved):
    tr.save_tasks(tmp_path, [])
    before = tr.tasks_file_path(tmp_path).read_bytes()
    parsed, validation = package(tasks)
    preview = ti.build_import_preview(tmp_path, parsed, validation,
                                      allow_unresolved_dependencies=allow_unresolved)
    assert preview.has_blocking_errors
    assert any('цикл' in issue.message for issue in preview.errors)
    with pytest.raises(ti.TaskImportError, match='цикл'):
        ti.apply_task_package(tmp_path, parsed, validation,
                              allow_unresolved_dependencies=allow_unresolved)
    assert tr.tasks_file_path(tmp_path).read_bytes() == before


def test_combined_graph_uses_stored_duplicate_edges_and_cross_project_dependency(tmp_path):
    tr.save_tasks(tmp_path, [task('B', ['A'], project='VOYN')])
    parsed, validation = package([task('A', ['B']), task('B')])
    assert ti.build_import_preview(tmp_path, parsed, validation).has_blocking_errors
    with pytest.raises(ti.TaskImportError, match='A → B → A'):
        ti.apply_task_package(tmp_path, parsed, validation)
    assert [t['id'] for t in tr.load_tasks(tmp_path)] == ['B']


def test_unrelated_legacy_cycle_and_ignored_duplicate_definition_do_not_block(tmp_path):
    tr.save_tasks(tmp_path, [task('X', ['Y']), task('Y', ['X']), task('B')])
    parsed, validation = package([task('A', ['B']), task('B', ['A'])])
    assert not ti.build_import_preview(tmp_path, parsed, validation).has_blocking_errors
    result = ti.apply_task_package(tmp_path, parsed, validation)
    assert result.imported_ids == ['A']
    assert result.skipped_duplicate_ids == ['B']
    assert next(t for t in tr.load_tasks(tmp_path) if t['id'] == 'B')['depends_on'] == []


def test_apply_rechecks_graph_after_preview(tmp_path):
    parsed, validation = package([task('A', ['B'])])
    tr.save_tasks(tmp_path, [task('B')])
    assert not ti.build_import_preview(tmp_path, parsed, validation).has_blocking_errors
    tr.save_tasks(tmp_path, [task('B', ['A'])])
    with pytest.raises(ti.TaskImportError, match='цикл'):
        ti.apply_task_package(tmp_path, parsed, validation)
    assert [t['id'] for t in tr.load_tasks(tmp_path)] == ['B']


def test_deep_acyclic_package_has_no_recursion_limit(tmp_path):
    tasks = [task(str(i), [str(i + 1)]) for i in range(1500)] + [task('1500')]
    parsed, validation = package(tasks)
    assert not ti.build_import_preview(tmp_path, parsed, validation).has_blocking_errors
    assert len(ti.apply_task_package(tmp_path, parsed, validation).imported_ids) == 1501


def test_concurrent_imports_cannot_jointly_create_cycle(tmp_path):
    tr.save_tasks(tmp_path, [])
    start = threading.Barrier(2)
    outcomes = []
    errors = []

    def run(task_id, dependency):
        parsed, validation = package([task(task_id, [dependency])])
        start.wait(timeout=5)
        try:
            outcomes.append(ti.apply_task_package(tmp_path, parsed, validation,
                                                 allow_unresolved_dependencies=True))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=('A', 'B')),
               threading.Thread(target=run, args=('B', 'A'))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert len(outcomes) == 1
    assert len(errors) == 1 and isinstance(errors[0], ti.TaskImportError)
    assert 'цикл' in str(errors[0])
    assert len(tr.load_tasks(tmp_path)) == 1


@pytest.mark.parametrize('cyclic', [False, True])
def test_remote_backend_retains_its_create_path_without_json_fallback(tmp_path, monkeypatch, cyclic):
    class RemoteRepository:
        created = []

        def load_all(self):
            return [task('B', ['A'] if cyclic else [])]

        def create(self, record, **kwargs):
            self.created.append(record)
            return record

    remote = RemoteRepository()
    monkeypatch.setattr(tr, 'get_repository', lambda root: remote)
    monkeypatch.setattr(tr, 'mutate_tasks', lambda *args, **kwargs: pytest.fail('JSON fallback'))
    parsed, validation = package([task('A', ['B'])])
    if cyclic:
        with pytest.raises(ti.TaskImportError, match='цикл'):
            ti.apply_task_package(tmp_path, parsed, validation)
        assert remote.created == []
    else:
        assert ti.apply_task_package(tmp_path, parsed, validation).imported_ids == ['A']
        assert [t['id'] for t in remote.created] == ['A']
