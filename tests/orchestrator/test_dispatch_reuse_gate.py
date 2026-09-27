"""The pre-dispatch reuse gate (VOYN-W0-AICC-DISPATCH-REUSE-GATE).

Live case, 2026-09-06: a `-REM` task's run re-implemented
`checkpoint_dirty_task_workspace`, which PR 624 had already merged to main.
The duplicate definitions collided and the merged result failed CI with
TypeErrors. The planner never asked whether the parent's acceptance was
already satisfied on main — it only asked what was eligible.

These tests fabricate that shape without a database: a real (tiny) git
repository supplies the merged history, and a scripted connection stands in
for the store, recording every call so "was a run dispatched?" is answerable
exactly. The gate must close a superseded remediation task with evidence and
never dispatch it — and, just as importantly, must abstain and dispatch
normally everywhere the proof is not conclusive.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from command_center.orchestrator import planner

PARENT = "VOYN-W0-AICC-WORKSPACE-CHECKPOINT"
REMEDIATION = f"{PARENT}-REM"
ORIGIN = "https://github.com/voyn88/ai-command-center.git"

# Commit clock. The parent task is "created" between the two, so a symbol
# that existed BEFORE it cannot be evidence that its work landed.
BEFORE_TASK = 1_757_000_000
TASK_CREATED = 1_757_100_000
AFTER_TASK = 1_757_200_000


def _git(repo: Path, args: list[str], when: int | None = None) -> None:
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(repo),
    }
    if when is not None:
        stamp = f"{when} +0000"
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    )


def _repository(tmp_path: Path, commits: list[tuple[str, str, int]]) -> Path:
    """A git repository on `main` whose history is `(subject, body, time)`.

    `body` is written to `module.py`, so a commit can introduce a symbol the
    gate's second signal then has to find.
    """
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, ["init", "-b", "main", "--quiet"])
    _git(repo, ["remote", "add", "origin", ORIGIN])
    for subject, content, when in commits:
        (repo / "module.py").write_text(content, encoding="utf-8")
        _git(repo, ["add", "module.py"])
        _git(repo, ["commit", "--quiet", "-m", subject], when=when)
    return repo


# --- the store, scripted -----------------------------------------------------

_DISPATCH_VERDICT = (True, "dispatched", "WI-1", 2)


class _Store:
    """Every planner query this tick makes, answered from a script.

    Routing is by the distinctive fragment of each statement rather than by
    call order, so the test does not silently pass when the planner starts
    asking a different question.
    """

    def __init__(
        self,
        *,
        candidates: list[tuple],
        parents: dict[str, tuple[str, str, int]],
        linked: dict[str, str] | None = None,
        close_verdict: tuple = (True, "superseded", 2),
    ) -> None:
        self.candidates = candidates
        self.parents = parents
        self.linked = linked or {}
        self.close_verdict = close_verdict
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple) -> list[tuple]:
        self.calls.append((sql, params))
        if "backlog_lease_acquire" in sql:
            return [(True, "acquired", 1)]
        if "backlog_lease_release" in sql:
            return [(True, "released", 1)]
        if "backlog_ingest_results" in sql:
            return []
        if "monitor_finding" in sql:
            return []
        if "FROM backlog_eligible" in sql:
            return self.candidates
        if "FROM backlog_task_remediation" in sql:
            return [(task, parent) for task, parent in self.linked.items()]
        if "EXTRACT(EPOCH FROM created_at)" in sql:
            row = self.parents.get(params[0])
            return [row] if row else []
        if "backlog_close_superseded" in sql:
            return [self.close_verdict]
        if "return_to_pool" in sql:  # _split_requested
            return []
        if "backlog_dispatch(" in sql:
            return [_DISPATCH_VERDICT]
        raise AssertionError(f"unscripted statement: {sql}")

    def called(self, fragment: str) -> list[tuple]:
        return [params for sql, params in self.calls if fragment in sql]


class _Cursor:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self._result: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._result = self._store.execute(sql, params)

    def fetchall(self) -> list[tuple]:
        return self._result

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc) -> None:
        return None


class _Connection:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def cursor(self) -> _Cursor:
        return _Cursor(self._store)

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_exc) -> None:
        return None


def _factory_for(store: _Store):
    @contextmanager
    def factory():
        yield _Connection(store)

    return factory


def _candidate(task_id: str, *, repo: str = "ai-command-center") -> tuple:
    # (task_id, wave, priority, title, body, repo, dispatchable, task_class)
    return (task_id, "0", "P0", f"title of {task_id}", "body", repo, True, "task")


_LIMITS = planner.PlanLimits(max_resumes_per_tick=0, review_backlog_limit=0)


def _plan(store: _Store, repo: Path | None):
    return planner.plan_once(
        _factory_for(store), _LIMITS, source_path=str(repo) if repo else None
    )


# --- the acceptance case -----------------------------------------------------


def test_a_remediation_whose_parent_is_merged_is_closed_without_dispatch(tmp_path):
    """The live incident, prevented: the parent's pull request is on main, so
    the remediation task closes as superseded and no run is dispatched."""
    repo = _repository(
        tmp_path,
        [
            ("initial", "x = 1\n", BEFORE_TASK),
            (f"{PARENT}: checkpoint the dirty task workspace (#624)", "x = 2\n", AFTER_TASK),
        ],
    )
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={PARENT: ("checkpoint the workspace", "body", TASK_CREATED)},
    )

    report = _plan(store, repo)

    assert store.called("backlog_dispatch(") == []
    (task_id, parent_id, pr_url, sha, evidence), = store.called(
        "backlog_close_superseded"
    )
    assert (task_id, parent_id) == (REMEDIATION, PARENT)
    assert pr_url == "https://github.com/voyn88/ai-command-center/pull/624"
    assert len(sha) == 40
    assert evidence.startswith(f"commit_title: {PARENT} is already on main as")
    assert report.superseded == [(REMEDIATION, evidence)]
    assert report.dispatched == []


def test_the_gate_counts_the_duplicate_dispatches_it_prevented(tmp_path):
    """The tick's telemetry: one number the operator can read, backed by the
    evidence behind every unit of it."""
    second_parent = "VOYN-W0-AICC-SECOND-THING"
    repo = _repository(
        tmp_path,
        [
            ("initial", "x = 1\n", BEFORE_TASK),
            (f"{PARENT}: land it (#624)", "x = 2\n", AFTER_TASK),
            (f"{second_parent}: land it too (#625)", "x = 3\n", AFTER_TASK),
        ],
    )
    store = _Store(
        candidates=[
            _candidate(REMEDIATION),
            _candidate(f"{second_parent}-RETRY"),
            _candidate("VOYN-W0-AICC-FRESH-WORK"),
        ],
        parents={
            PARENT: ("t", "b", TASK_CREATED),
            second_parent: ("t", "b", TASK_CREATED),
        },
    )

    report = _plan(store, repo)

    assert report.prevented_duplicate_dispatches == 2
    assert [task for task, _evidence in report.superseded] == [
        REMEDIATION,
        f"{second_parent}-RETRY",
    ]
    # The ordinary task in the same tick is untouched by the gate.
    assert [task for task, _work_item in report.dispatched] == [
        "VOYN-W0-AICC-FRESH-WORK"
    ]


# --- abstention: the gate may only ever prevent PROVABLE duplicates ----------


def test_an_ordinary_task_is_never_examined_by_the_gate(tmp_path):
    """A task that is not a remediation of anything is dispatched as before."""
    repo = _repository(tmp_path, [("VOYN-W0-AICC-THING: done (#1)", "x = 1\n", AFTER_TASK)])
    store = _Store(candidates=[_candidate("VOYN-W0-AICC-THING")], parents={})

    report = _plan(store, repo)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == ["VOYN-W0-AICC-THING"]


def test_a_remediation_whose_parent_is_not_on_main_is_dispatched(tmp_path):
    """The ordinary case: the parent was rejected, nothing merged, the
    remediation is exactly the work that still has to happen."""
    repo = _repository(tmp_path, [("something else entirely (#7)", "x = 1\n", AFTER_TASK)])
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={PARENT: ("t", "b", TASK_CREATED)},
    )

    report = _plan(store, repo)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


def test_the_remediations_own_merge_is_not_evidence_about_its_parent(tmp_path):
    """`VOYN-X-REM` in a commit title is not `VOYN-X` — a substring match here
    would close a task on the strength of its own earlier attempt."""
    repo = _repository(
        tmp_path, [(f"{REMEDIATION}: a second attempt (#636)", "x = 1\n", AFTER_TASK)]
    )
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={PARENT: ("t", "b", TASK_CREATED)},
    )

    report = _plan(store, repo)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


def test_a_merge_without_a_pull_request_number_is_not_evidence(tmp_path):
    """The criterion is a merged PULL REQUEST; a subject with no number
    cannot be cited as one, so the gate abstains rather than invent it."""
    repo = _repository(tmp_path, [(f"{PARENT}: landed by hand", "x = 1\n", AFTER_TASK)])
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={PARENT: ("t", "b", TASK_CREATED)},
    )

    report = _plan(store, repo)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


def test_a_repository_this_host_cannot_read_is_never_judged(tmp_path):
    """The checkout is `ai-command-center`; a candidate for another repository
    must not be judged against this history."""
    repo = _repository(tmp_path, [(f"{PARENT}: landed (#624)", "x = 1\n", AFTER_TASK)])
    store = _Store(
        candidates=[_candidate(REMEDIATION, repo="~/Projects/aios")],
        parents={PARENT: ("t", "b", TASK_CREATED)},
    )

    report = _plan(store, repo)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


def test_the_backlog_repo_hint_matches_a_path_shaped_name(tmp_path):
    """`~/Projects/ai-command-center` is the same repository as
    `ai-command-center` — the backlog writes both."""
    repo = _repository(tmp_path, [(f"{PARENT}: landed (#624)", "x = 1\n", AFTER_TASK)])
    store = _Store(
        candidates=[_candidate(REMEDIATION, repo="~/Projects/ai-command-center")],
        parents={PARENT: ("t", "b", TASK_CREATED)},
    )

    report = _plan(store, repo)

    assert len(store.called("backlog_close_superseded")) == 1
    assert report.dispatched == []


def test_without_a_source_path_the_gate_is_off(tmp_path):
    """No checkout, no git, no gate: the tick behaves exactly as before."""
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={PARENT: ("t", "b", TASK_CREATED)},
    )

    report = _plan(store, None)

    assert store.called("FROM backlog_task_remediation") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


def test_a_directory_that_is_not_a_repository_abstains(tmp_path):
    """A control host without a readable checkout dispatches as before rather
    than failing the tick."""
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={PARENT: ("t", "b", TASK_CREATED)},
    )

    report = _plan(store, tmp_path)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


# --- the second signal: the named symbols are already on main ---------------


def test_a_symbol_the_parent_named_and_main_gained_later_closes_the_task(tmp_path):
    """The incident's own shape: the acceptance named
    `checkpoint_dirty_task_workspace`, and another pull request merged it
    first — under a commit title that names a different task."""
    repo = _repository(
        tmp_path,
        [
            ("initial (#1)", "x = 1\n", BEFORE_TASK),
            (
                "VOYN-W0-AICC-SOMETHING-ELSE: add the checkpoint (#624)",
                "def checkpoint_dirty_task_workspace(path):\n    return path\n",
                AFTER_TASK,
            ),
        ],
    )
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={
            PARENT: (
                "checkpoint the workspace",
                "Acceptance: `checkpoint_dirty_task_workspace` exists and is called.",
                TASK_CREATED,
            )
        },
    )

    report = _plan(store, repo)

    (_task, _parent, pr_url, _sha, evidence), = store.called(
        "backlog_close_superseded"
    )
    assert pr_url == "https://github.com/voyn88/ai-command-center/pull/624"
    assert evidence.startswith("symbols:")
    assert "checkpoint_dirty_task_workspace" in evidence
    assert report.prevented_duplicate_dispatches == 1


def test_a_symbol_that_predates_the_parent_task_is_not_evidence(tmp_path):
    """Naming something that already existed is a reference, not a
    deliverable — closing on it would abandon real work."""
    repo = _repository(
        tmp_path,
        [
            (
                "much earlier work (#1)",
                "def checkpoint_dirty_task_workspace(path):\n    return path\n",
                BEFORE_TASK,
            ),
            ("unrelated later change (#2)", "y = 1\n", AFTER_TASK),
        ],
    )
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={
            PARENT: (
                "checkpoint the workspace",
                "Acceptance: `checkpoint_dirty_task_workspace` is called on failure.",
                TASK_CREATED,
            )
        },
    )

    report = _plan(store, repo)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


def test_every_named_symbol_must_be_present(tmp_path):
    """Half a deliverable is not a superseded task."""
    repo = _repository(
        tmp_path,
        [
            (
                "VOYN-W0-AICC-OTHER: half of it (#624)",
                "def checkpoint_dirty_task_workspace(path):\n    return path\n",
                AFTER_TASK,
            )
        ],
    )
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={
            PARENT: (
                "checkpoint the workspace",
                "Acceptance: `checkpoint_dirty_task_workspace` and "
                "`restore_dirty_task_workspace` both exist.",
                TASK_CREATED,
            )
        },
    )

    report = _plan(store, repo)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


def test_a_body_naming_too_many_symbols_abstains():
    """Past a handful, "all of them are present" stops being a statement
    about this task's deliverable."""
    body = " ".join(f"`symbol_number_{index}`" for index in range(6))
    assert planner.acceptance_symbols("t", body) == ()
    assert planner.acceptance_symbols("t", "nothing named here") == ()


def test_a_symbol_mentioned_but_never_defined_is_not_present(tmp_path):
    """A name in a comment is not a definition."""
    repo = _repository(
        tmp_path,
        [
            (
                "VOYN-W0-AICC-OTHER: mention only (#624)",
                "# checkpoint_dirty_task_workspace is planned\nx = 1\n",
                AFTER_TASK,
            )
        ],
    )
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={
            PARENT: ("t", "Acceptance: `checkpoint_dirty_task_workspace`.", TASK_CREATED)
        },
    )

    report = _plan(store, repo)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


# --- lineage and refusals ----------------------------------------------------


def test_the_recorded_lineage_names_the_parent_when_the_id_does_not(tmp_path):
    """A remediation task whose id carries no suffix is still a remediation:
    `backlog_task_remediation` is the authority."""
    child = "VOYN-W0-AICC-FOLLOW-UP-WORK"
    repo = _repository(tmp_path, [(f"{PARENT}: landed (#624)", "x = 1\n", AFTER_TASK)])
    store = _Store(
        candidates=[_candidate(child)],
        parents={PARENT: ("t", "b", TASK_CREATED)},
        linked={child: PARENT},
    )

    report = _plan(store, repo)

    (task_id, parent_id, _pr, _sha, _evidence), = store.called(
        "backlog_close_superseded"
    )
    assert (task_id, parent_id) == (child, PARENT)
    assert report.dispatched == []


def test_a_refused_close_is_reported_and_still_not_dispatched(tmp_path):
    """The store is the authority. If it refuses, the tick says so by name
    and leaves the task OPEN — it does not dispatch a run it has proof is
    redundant."""
    repo = _repository(tmp_path, [(f"{PARENT}: landed (#624)", "x = 1\n", AFTER_TASK)])
    store = _Store(
        candidates=[_candidate(REMEDIATION)],
        parents={PARENT: ("t", "b", TASK_CREATED)},
        close_verdict=(False, "not_open", 3),
    )

    report = _plan(store, repo)

    assert report.superseded == []
    assert report.refused == [(REMEDIATION, "supersede_refused:not_open")]
    assert report.dispatched == []


def test_an_unknown_parent_task_abstains(tmp_path):
    """The suffix says "remediation", the store has no such parent: nothing
    to compare against, so the task is dispatched."""
    repo = _repository(tmp_path, [(f"{PARENT}: landed (#624)", "x = 1\n", AFTER_TASK)])
    store = _Store(candidates=[_candidate(REMEDIATION)], parents={})

    report = _plan(store, repo)

    assert store.called("backlog_close_superseded") == []
    assert [task for task, _work_item in report.dispatched] == [REMEDIATION]


# --- the pure helpers --------------------------------------------------------


@pytest.mark.parametrize(
    ("task_id", "expected"),
    [
        ("VOYN-W0-X-REM", "VOYN-W0-X"),
        ("VOYN-W0-X-RETRY", "VOYN-W0-X"),
        ("VOYN-W0-X-REM-REM", "VOYN-W0-X-REM"),
        ("VOYN-W0-X", None),
        ("-REM", None),
    ],
)
def test_remediation_parent_reads_the_suffix_convention(task_id, expected):
    assert planner.remediation_parent(task_id) == expected


def test_recorded_lineage_wins_over_the_suffix():
    assert (
        planner.remediation_parent("VOYN-W0-X-REM", "VOYN-W0-OTHER") == "VOYN-W0-OTHER"
    )
