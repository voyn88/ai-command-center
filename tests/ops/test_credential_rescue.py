"""Bounded rescue decisions without a PostgreSQL dependency."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from command_center.ops import credential_rescue as rescue

NOW = datetime(2026, 10, 30, tzinfo=UTC)
EXPIRED = rescue.Worker(
    "wrk:example",
    "active",
    "aicc_w_example",
    "credential-1",
    NOW - timedelta(hours=1),
    False,
    NOW,
    0,
    None,
)


class FakeRepository:
    def __init__(self, worker=EXPIRED):
        self.worker = worker
        self.writes = []
        self.locked = False

    def workers(self):
        return [self.worker.principal_id]

    @contextmanager
    def locked_worker(self, principal_id):
        assert principal_id == self.worker.principal_id
        self.locked = True
        try:
            yield self.worker
        finally:
            self.locked = False

    def extend(self, worker, grace_seconds):
        assert self.locked
        self.writes.append(
            (worker.credential_id, worker.now + timedelta(seconds=grace_seconds))
        )


@pytest.mark.parametrize(
    "changes,reason",
    [
        (
            {"has_valid_credential": True, "expires_at": NOW + timedelta(minutes=5)},
            "valid_credential_exists",
        ),
        ({"has_valid_credential": True}, "valid_credential_exists"),
        ({"expires_at": NOW - timedelta(seconds=599)}, "recently_expired"),
        ({"state": "suspended"}, "principal_inactive"),
        ({"state": "retired"}, "principal_inactive"),
        ({"credential_id": None, "expires_at": None}, "no_non_revoked_credential"),
    ],
)
def test_noop(changes, reason, capsys):
    repository = FakeRepository(replace(EXPIRED, **changes))
    assert rescue.run(repository, rescue.RescueConfig()) == 0
    assert not repository.writes
    assert capsys.readouterr().out.strip() == (
        f"NOOP wrk:example {repository.worker.credential_id or '-'} {reason}"
    )


@pytest.mark.parametrize(
    "requested,effective", [(2040, 2040), (1200, 1200), (1, 300), (9999, 2040)]
)
def test_rescue_and_grace_clamp(requested, effective, capsys):
    repository = FakeRepository()
    assert rescue.run(repository, rescue.RescueConfig(grace_seconds=requested)) == 0
    assert repository.writes == [("credential-1", NOW + timedelta(seconds=effective))]
    assert capsys.readouterr().out == (
        "RESCUED wrk:example credential-1 expired_credential_rescued\n"
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"last_rescue": NOW - timedelta(hours=1), "rescues_today": 1}, "cooldown"),
        ({"last_rescue": NOW - timedelta(hours=7), "rescues_today": 3}, "daily_limit"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_refused(changes, reason, dry_run, capsys):
    repository = FakeRepository(replace(EXPIRED, **changes))
    assert rescue.run(repository, rescue.RescueConfig(dry_run=dry_run)) == 3
    assert not repository.writes
    assert capsys.readouterr().out == f"REFUSED wrk:example credential-1 {reason}\n"


def test_dry_run_writes_nothing(capsys):
    repository = FakeRepository()
    assert rescue.run(repository, rescue.RescueConfig(dry_run=True)) == 0
    assert not repository.writes
    assert capsys.readouterr().out == (
        "DRYRUN wrk:example credential-1 expired_credential_rescued\n"
    )


def test_exact_bounds_allow_rescue():
    repository = FakeRepository(
        replace(
            EXPIRED,
            expires_at=NOW - timedelta(seconds=600),
            last_rescue=NOW - timedelta(seconds=21600),
            rescues_today=2,
        )
    )
    assert rescue.run(repository, rescue.RescueConfig()) == 0
    assert len(repository.writes) == 1


@pytest.mark.parametrize(
    "settings",
    [
        {"min_expired_seconds": -1},
        {"cooldown_seconds": -1},
        {"max_per_day": 0},
    ],
)
def test_invalid_bounds(settings):
    with pytest.raises(ValueError):
        rescue.RescueConfig(**settings)


def test_main_errors_are_nonzero_and_redacted(monkeypatch, capsys):
    def fail():
        raise RuntimeError("sensitive connection details")

    monkeypatch.setattr(rescue, "load_config", fail)
    assert rescue.main([]) == 1
    assert "sensitive" not in capsys.readouterr().err
