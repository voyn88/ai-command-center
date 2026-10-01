from __future__ import annotations

from contextlib import contextmanager

import pytest

from command_center.db import adapter, pool
from command_center.db.config import PostgresConfig


class FakeCursor:
    def __init__(self, session_user: str) -> None:
        self._session_user = session_user

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        pass

    def fetchone(self) -> tuple[str]:
        return (self._session_user,)


class FakeIdentityConnection:
    def __init__(self, session_user: str) -> None:
        self._session_user = session_user

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._session_user)


class FakeBuiltPool:
    """Stands in for what `adapter.open_pool()` returns: a real pool object
    whose `.connection()` yields something with a `.cursor()`, unlike
    `FakePool` below which stands in for `pool._build_pool()`'s own return
    value and is deliberately cruder."""

    def __init__(self, session_user: str) -> None:
        self._session_user = session_user
        self.closed = False

    @contextmanager
    def connection(self):
        yield FakeIdentityConnection(self._session_user)

    def close(self) -> None:
        self.closed = True


class FakePool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    @contextmanager
    def connection(self):
        yield self.name

    def close(self) -> None:
        self.closed = True


def _config(password: str) -> PostgresConfig:
    return PostgresConfig(
        host="127.0.0.1",
        port=5432,
        dbname="aicc",
        user="aicc_worker",
        password=password,
        sslmode="disable",
        sslrootcert=None,
        connect_timeout=5,
        application_name="test",
        pool_min_size=1,
        pool_max_size=2,
        pool_timeout_seconds=5,
        statement_timeout_ms=30_000,
    )


def test_replace_pool_keeps_checked_out_old_generation_until_return(
    monkeypatch,
) -> None:
    old = FakePool("old")
    new = FakePool("new")
    generations = iter((old, new))
    monkeypatch.setattr(pool, "_build_pool", lambda config: next(generations))
    pool.close_pool()
    try:
        pool.open_pool(_config("a" * 64))
        checkout = pool.connection()
        assert checkout.__enter__() == "old"

        pool.replace_pool(_config("b" * 64))
        assert not old.closed, "an active heartbeat checkout must not be cut"
        with pool.connection() as value:
            assert value == "new"

        checkout.__exit__(None, None, None)
        assert old.closed, "the retired generation closes after its last return"
        assert not new.closed
    finally:
        pool.close_pool()


def test_failed_replacement_leaves_current_pool_usable(monkeypatch) -> None:
    old = FakePool("old")
    monkeypatch.setattr(pool, "_build_pool", lambda config: old)
    pool.close_pool()
    try:
        pool.open_pool(_config("a" * 64))

        def fail(config):
            raise ConnectionError("new credential refused")

        monkeypatch.setattr(pool, "_build_pool", fail)
        try:
            pool.replace_pool(_config("b" * 64))
        except ConnectionError:
            pass
        else:
            raise AssertionError("replacement failure was swallowed")

        with pool.connection() as value:
            assert value == "old"
        assert not old.closed
    finally:
        pool.close_pool()


def test_close_pool_during_inflight_checkout_does_not_raise(monkeypatch) -> None:
    """close_pool() clearing the bookkeeping while a connection() checkout is
    still inside its context must not turn the checkout's finally-block into
    a KeyError: shutdown concurrent with in-flight work is the scenario
    rotation makes routine (independent-review finding on 2d5687c)."""
    fake = FakePool("only")
    monkeypatch.setattr(pool, "_build_pool", lambda config: fake)
    pool.close_pool()
    pool.open_pool(_config("a" * 64))
    checkout = pool.connection()
    assert checkout.__enter__() == "only"
    pool.close_pool()
    assert fake.closed, "close_pool() owns shutdown of every pool"
    # The regression: this __exit__ raised KeyError before the fix.
    checkout.__exit__(None, None, None)


def test_stale_checkout_unwind_cannot_touch_a_reincarnated_pool(monkeypatch) -> None:
    """id()-keyed bookkeeping was an ABA hazard: after close_pool() and GC, a
    NEW pool could reuse the dead pool's address, and the dead checkout's
    finally-block would decrement the new pool's counter (independent-review
    finding on d6fa8be). Generation tokens make the two pools distinct keys."""
    first = FakePool("first")
    second = FakePool("second")
    generations = iter((first, second))
    monkeypatch.setattr(pool, "_build_pool", lambda config: next(generations))
    pool.close_pool()
    pool.open_pool(_config("a" * 64))
    stale = pool.connection()
    assert stale.__enter__() == "first"
    first_token = pool._pool[0]
    pool.close_pool()
    pool.open_pool(_config("b" * 64))
    live = pool.connection()
    assert live.__enter__() == "second"
    second_token = pool._pool[0]
    # The discriminating assertions target the KEY itself: under id()-keyed
    # bookkeeping two co-resident FakePools can never collide, so only
    # asserting on close behaviour would pass on the buggy implementation
    # too (review finding on the first version of this test). Generation
    # tokens must differ across reopen, and the stale unwind must leave the
    # live token's count untouched.
    assert first_token != second_token
    assert pool._active == {second_token: 1}
    # The dead checkout unwinds AFTER the new pool has a live checkout.
    stale.__exit__(None, None, None)
    assert pool._active == {second_token: 1}, "stale unwind touched live key"
    # If the stale unwind had decremented the live token, replace_pool would
    # see zero active checkouts and close "second" mid-checkout.
    third = FakePool("third")
    monkeypatch.setattr(pool, "_build_pool", lambda config: third)
    pool.replace_pool(_config("c" * 64))
    assert not second.closed, "live checkout must keep its pool open"
    live.__exit__(None, None, None)
    assert second.closed, "retired pool closes after its last return"
    pool.close_pool()


def test_replace_pool_refuses_to_resurrect_a_concurrently_closed_pool(
    monkeypatch,
) -> None:
    """close_pool() racing _build_pool() must not let replace_pool() install a
    live pool after shutdown (independent-review finding on 0e3dad6): the
    generation captured before the build is re-checked under the lock, and a
    mismatch discards the freshly built replacement."""
    first = FakePool("first")
    replacement = FakePool("replacement")

    def build_then_shutdown(config):
        if not first_built:
            return first
        pool.close_pool()  # the race: shutdown lands mid-build
        return replacement

    first_built = False
    monkeypatch.setattr(pool, "_build_pool", build_then_shutdown)
    pool.close_pool()
    pool.open_pool(_config("a" * 64))
    first_built = True
    try:
        pool.replace_pool(_config("b" * 64))
    except pool.PoolReplacedError:
        pass
    else:
        raise AssertionError("replacement must be refused after shutdown")
    assert replacement.closed, "orphaned replacement must be closed"
    assert pool._pool is None, "shutdown must stay shut down"


def test_build_pool_accepts_a_session_user_matching_the_authenticated_role(
    monkeypatch,
) -> None:
    built = FakeBuiltPool("aicc_worker")
    monkeypatch.setattr(adapter, "open_pool", lambda *a, **kw: built)
    result = pool._build_pool(_config("a" * 64))
    assert result is built
    assert not built.closed


def test_build_pool_refuses_a_pooler_that_hides_the_authenticated_role(
    monkeypatch,
) -> None:
    """A transaction-mode pooler in front of PostgreSQL can authenticate
    under its own shared role and hand every caller that role's
    `session_user` back, regardless of who connected to the pooler. That
    silently breaks the session_user-is-the-claimant identity model (see
    `command_center/db/pool.py`'s module docstring), so `_build_pool()` must
    fail startup instead of returning a pool that will make every claim
    unattributable."""
    built = FakeBuiltPool("shared_pooler_role")
    monkeypatch.setattr(adapter, "open_pool", lambda *a, **kw: built)
    with pytest.raises(pool.PoolIdentityError):
        pool._build_pool(_config("a" * 64))
    assert built.closed, "a pool that fails the identity check must not leak"
