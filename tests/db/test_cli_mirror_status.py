"""`python -m command_center.db mirror-status` (VOYN-W0-AICC-SRV-09-READ-POOL).

The blocking defect on the first attempt (adversarial review of PR #438):
`mirror-status` ran inside the CLI's own `with pool.connection()`, then each
`Postgres*Mirror.list_records()` it drove checked out *another* connection.
With the valid configuration `AICC_PG_POOL_MAX=1` that second checkout has
nothing to draw from — the command hangs until it times out instead of
reporting status. `test_execution_reconcile.py` calls
`reconcile_execution_center()` directly with one shared, injected factory, so
it cannot see this: the defect is specifically in what the CLI passes as that
factory, which is what this test drives instead.

No PostgreSQL needed: `pool.connection` is replaced with a fake that models a
size-1 pool (raises if asked for a second connection while the first is still
checked out), and the mirrors' own `list_records()` runs for real against it —
only the network driver underneath is fake.
"""

from __future__ import annotations

import pytest

import command_center.db.cli as cli_module
from command_center.runtime.db import execution as exec_db


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, *args, **kwargs) -> None:
        return None

    def fetchall(self) -> list[tuple]:
        return []


class _SizeOnePool:
    """Models `AICC_PG_POOL_MAX=1`: a second concurrent checkout fails.

    Not a context manager itself — `pool.connection()` is a function that
    returns a *new* context manager each call, and a size-1 pool handing out
    two of those concurrently is exactly the exhaustion this reproduces.
    """

    def __init__(self) -> None:
        self._checked_out = False
        self.max_concurrent = 0
        self.checkouts = 0

    def connection(self):
        return self._checkout()

    def _checkout(self):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            if self._checked_out:
                raise TimeoutError(
                    "pool exhausted: AICC_PG_POOL_MAX=1 and a connection is "
                    "already checked out"
                )
            self._checked_out = True
            self.checkouts += 1
            self.max_concurrent = max(self.max_concurrent, 1)
            try:
                yield self
            finally:
                self._checked_out = False

        return _cm()

    def cursor(self):
        return _FakeCursor()


@pytest.fixture
def size_one_pool(monkeypatch):
    fake = _SizeOnePool()
    monkeypatch.setattr(cli_module, "load_config", lambda: object())
    monkeypatch.setattr(cli_module.pool, "open_pool", lambda config=None: None)
    monkeypatch.setattr(cli_module.pool, "close_pool", lambda: None)
    monkeypatch.setattr(cli_module.pool, "connection", fake.connection)
    return fake


def test_mirror_status_parses_with_a_default_db_path() -> None:
    args = cli_module.build_parser().parse_args(["mirror-status"])
    assert args.command == "mirror-status"
    assert args.db_path is None


def test_mirror_status_does_not_exhaust_a_size_one_pool(
    size_one_pool, tmp_path
) -> None:
    """The regression test for PR #438's rejection.

    An empty, freshly migrated authority is enough: `divergence()` calls
    `mirror.list_records()` unconditionally, so the second-checkout bug fires
    on an empty database exactly as it would on a populated one.
    """
    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)

    exit_code = cli_module.main(["mirror-status", "--db-path", str(db_path)])

    assert exit_code == 0
    assert size_one_pool.max_concurrent == 1
    # Both mirrors' `list_records()` ran, and neither needed a checkout of its
    # own: the CLI's single connection is the only one ever handed out.
    assert size_one_pool.checkouts == 1


def test_mirror_status_reports_a_nonzero_exit_on_divergence(
    size_one_pool, tmp_path
) -> None:
    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    exec_db.create_task(db_path, project="AICC", title="never mirrored", task_type="feature")

    exit_code = cli_module.main(["mirror-status", "--db-path", str(db_path)])

    assert exit_code == 1
    assert size_one_pool.max_concurrent == 1
