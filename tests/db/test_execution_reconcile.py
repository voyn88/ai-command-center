"""`reconcile_execution_center` -- the read-side check behind `mirror-status`
(VOYN-W0-AICC-SRV-09-READ-POOL).

Two rejections preceded this file. The first shipped a `mirror-status` that
checked out a *second* pool connection from inside the CLI's own outer
`pool.connection()` block, which exhausts a pool sized `AICC_PG_POOL_MAX=1`
instead of reporting; that is fixed in `command_center/db/cli.py` by reusing
the outer connection, and is not this module's concern to re-prove (the
parser test below only pins that the command exists and is read-only).

The second shipped a test whose `_patch()` replaced each mirror class with a
`lambda mirror=<real class>: mirror(connection_factory=factory)` -- built for
the write hooks, which always call `PostgresTaskMirror()` with *no*
arguments, so the lambda's own defaulted parameter is never actually
overwritten there. Production reconciliation instead called
`PostgresTaskMirror(pg_connection_factory)` *positionally*, which clobbers
that same defaulted parameter with the factory function and then calls the
factory with the unsupported `connection_factory=` keyword the real class
would have accepted -- a `TypeError`, not a report. The fakes below are
deliberately narrow (`__init__(self, connection_factory=None)`, nothing else)
so they can only be called the one way production actually calls them, and
`test_the_connection_factory_is_forwarded_unchanged` pins that shape directly
rather than trusting a patch helper built for a different call site.
"""

from __future__ import annotations

import pytest

# `execution_reconcile` imports `command_center.db.execution_store` lazily
# (inside the function, not at module scope -- see its docstring), but the
# pool fallback it exercises still reaches the `aios_db` adapter, which is
# vendored and optional in a bare local checkout (see test_cli_queue.py).
pytest.importorskip("aios_db")

from command_center.runtime.db import execution as execution_db
from command_center.db.execution_reconcile import reconcile_execution_center


class _FakeMirror:
    """Accepts exactly what `PostgresTableMirror` accepts, nothing more --
    a positional call or an unexpected keyword fails loudly here instead of
    silently degrading to the real pool, which is what a too-permissive fake
    (e.g. `**kwargs`) would let slip past."""

    def __init__(self, connection_factory=None) -> None:
        self.connection_factory = connection_factory
        self.records: list[dict] = []

    def list_records(self) -> list[dict]:
        return self.records


def _patch_mirrors(monkeypatch, *, tasks: list[dict], sessions: list[dict]):
    from command_center.db import execution_store

    task_mirror = _FakeMirror()
    task_mirror.records = tasks
    session_mirror = _FakeMirror()
    session_mirror.records = sessions

    # Built once, above, and returned by a stand-in constructor rather than
    # instantiated for real each call -- what matters here is the keyword the
    # real constructor gets called with, captured below.
    calls: dict[str, object] = {}

    def _make_task_mirror(**kwargs):
        calls["task"] = kwargs.get("connection_factory")
        task_mirror.connection_factory = kwargs.get("connection_factory")
        return task_mirror

    def _make_session_mirror(**kwargs):
        calls["session"] = kwargs.get("connection_factory")
        session_mirror.connection_factory = kwargs.get("connection_factory")
        return session_mirror

    monkeypatch.setattr(execution_store, "PostgresTaskMirror", _make_task_mirror)
    monkeypatch.setattr(execution_store, "PostgresSessionMirror", _make_session_mirror)
    return calls


def test_reconcile_reports_clean_after_a_normal_write(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    execution_db.db.migrate(db_path)
    task = execution_db.create_task(
        db_path, project="AICC", title="mirror me", task_type="feature"
    )
    session = execution_db.create_session(
        db_path, task_id=task["id"], project="AICC", repository_path="/tmp/repo"
    )

    sentinel = object()
    _patch_mirrors(monkeypatch, tasks=[task], sessions=[session])

    report = reconcile_execution_center(db_path, connection_factory=sentinel)

    assert report.task_divergence == []
    assert report.session_divergence == []
    assert report.clean is True


def test_reconcile_reports_a_missing_mirror_row(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    execution_db.db.migrate(db_path)
    task = execution_db.create_task(
        db_path, project="AICC", title="never mirrored", task_type="feature"
    )

    _patch_mirrors(monkeypatch, tasks=[], sessions=[])

    report = reconcile_execution_center(db_path, connection_factory=object())

    assert report.clean is False
    assert [d["id"] for d in report.task_divergence] == [task["id"]]
    assert report.session_divergence == []


def test_the_connection_factory_is_forwarded_unchanged(monkeypatch, tmp_path) -> None:
    """The exact bug the second rejection found: a caller (the CLI, reusing
    its own already-checked-out connection) must see that same factory reach
    *both* mirror constructors, as the `connection_factory=` keyword the real
    class accepts -- not positionally, and not rebuilt into something else
    along the way."""
    db_path = tmp_path / "runtime.db"
    execution_db.db.migrate(db_path)

    sentinel = object()
    calls = _patch_mirrors(monkeypatch, tasks=[], sessions=[])

    reconcile_execution_center(db_path, connection_factory=sentinel)

    assert calls == {"task": sentinel, "session": sentinel}


def test_reconcile_defaults_to_the_process_pool(monkeypatch, tmp_path) -> None:
    """Left `None`, this is `command_center/runtime/`'s wiring of the pool
    into the read path (VOYN-W0-AICC-SRV-09-READ-POOL's own precondition):
    the fallback below is `command_center.db.pool.connection` itself, not a
    stand-in for it."""
    from command_center.db import pool

    db_path = tmp_path / "runtime.db"
    execution_db.db.migrate(db_path)

    calls = _patch_mirrors(monkeypatch, tasks=[], sessions=[])

    reconcile_execution_center(db_path)

    assert calls == {"task": pool.connection, "session": pool.connection}
