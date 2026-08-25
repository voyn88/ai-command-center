from __future__ import annotations

from contextlib import contextmanager

from command_center.db import pool
from command_center.db.config import PostgresConfig


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
