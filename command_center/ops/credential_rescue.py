"""Bounded control-plane recovery for workers whose database secret expired.

The worker rotator cannot authenticate after expiry. An administrator session
briefly reopens the latest credential so that normal rotation can take over.
The ledger, role deadline and audit record commit together; repeated rescues
refuse rather than silently turning a short-lived credential into a permanent one.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from command_center.db.config import load_config


@dataclass(frozen=True, slots=True)
class RescueConfig:
    grace_seconds: int = 2040
    min_expired_seconds: int = 600
    cooldown_seconds: int = 21600
    max_per_day: int = 3
    dry_run: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "grace_seconds", min(2040, max(300, self.grace_seconds))
        )
        if self.min_expired_seconds < 0 or self.cooldown_seconds < 0:
            raise ValueError("expiry delay and cooldown must be nonnegative")
        if self.max_per_day < 1:
            raise ValueError("max-per-day must be positive")


@dataclass(frozen=True, slots=True)
class Worker:
    principal_id: str
    state: str
    db_role: str
    credential_id: str | None
    expires_at: datetime | None
    has_valid_credential: bool
    now: datetime
    rescues_today: int
    last_rescue: datetime | None


@dataclass(frozen=True, slots=True)
class Result:
    status: str
    principal_id: str
    credential_id: str | None
    reason: str

    def line(self) -> str:
        return (
            f"{self.status} {self.principal_id} "
            f"{self.credential_id or '-'} {self.reason}"
        )


class Repository(Protocol):
    def workers(self) -> list[str]: ...

    def locked_worker(self, principal_id: str): ...

    def extend(self, worker: Worker, grace_seconds: int) -> None: ...


def decide(worker: Worker, config: RescueConfig) -> Result:
    """Evaluate a locked, server-clock snapshot without touching PostgreSQL."""
    status = "NOOP"
    if worker.state != "active":
        reason = "principal_inactive"
    elif worker.has_valid_credential:
        reason = "valid_credential_exists"
    elif worker.credential_id is None or worker.expires_at is None:
        reason = "no_non_revoked_credential"
    elif (worker.now - worker.expires_at).total_seconds() < config.min_expired_seconds:
        reason = "recently_expired"
    elif worker.rescues_today >= config.max_per_day:
        status, reason = "REFUSED", "daily_limit"
    elif (
        worker.last_rescue is not None
        and (worker.now - worker.last_rescue).total_seconds() < config.cooldown_seconds
    ):
        status, reason = "REFUSED", "cooldown"
    else:
        status = "DRYRUN" if config.dry_run else "RESCUED"
        reason = "expired_credential_rescued"
    return Result(status, worker.principal_id, worker.credential_id, reason)


def run(repository: Repository, config: RescueConfig) -> int:
    """Print committed outcomes and keep processing after a bounded refusal."""
    exit_code = 0
    for principal_id in repository.workers():
        with repository.locked_worker(principal_id) as worker:
            result = decide(worker, config)
            if result.status == "RESCUED":
                repository.extend(worker, config.grace_seconds)
        print(result.line(), flush=True)
        if result.status == "REFUSED":
            exit_code = 3
    return exit_code


class AdminSession:
    """All rescue SQL over one admin connection (autocommit; explicit transactions)."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def workers(self) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT principal_id FROM principal "
                "WHERE kind = 'worker_host' ORDER BY principal_id"
            ).fetchall()
        ]

    @contextmanager
    def locked_worker(self, principal_id: str):
        with self.connection.transaction():
            # Issuance and audit also lock the principal. Re-read credentials
            # after acquiring it so a concurrent rescue/successor is visible.
            principal = self.connection.execute(
                "SELECT state, db_role FROM principal "
                "WHERE principal_id = %s AND kind = 'worker_host' FOR UPDATE",
                (principal_id,),
            ).fetchone()
            if principal is None:
                raise RuntimeError("worker disappeared during rescue")
            credentials = self.connection.execute(
                "SELECT credential_id, expires_at FROM principal_credential "
                "WHERE principal_id = %s AND revoked_at IS NULL "
                "ORDER BY issued_at DESC, created_at DESC, credential_id DESC "
                "FOR UPDATE",
                (principal_id,),
            ).fetchall()
            now, count, last = self.connection.execute(
                "SELECT now(), count(*) FILTER "
                "(WHERE created_at > now() - interval '24 hours'), max(created_at) "
                "FROM principal_event WHERE principal_id = %s "
                "AND event_type = 'rescue' AND outcome = 'granted' "
                "AND reason = 'expired_credential_rescued'",
                (principal_id,),
            ).fetchone()
            latest = credentials[0] if credentials else (None, None)
            yield Worker(
                principal_id,
                *principal,
                *latest,
                any(expiry > now for _, expiry in credentials),
                now,
                count,
                last,
            )

    def extend(self, worker: Worker, grace_seconds: int) -> None:
        new_expiry = worker.now + timedelta(seconds=grace_seconds)
        self.connection.execute(
            "UPDATE principal_credential SET expires_at = %s, updated_at = now() "
            "WHERE credential_id = %s AND revoked_at IS NULL",
            (new_expiry, worker.credential_id),
        )
        # The server quotes the role name and deadline (format %I / %L), so the
        # statement never splices client text into DDL.
        (statement,) = self.connection.execute(
            "SELECT format('ALTER ROLE %%I VALID UNTIL %%L', %s::text, %s::text)",
            (worker.db_role, new_expiry.isoformat()),
        ).fetchone()
        self.connection.execute(statement)
        self.connection.execute(
            "SELECT _principal_audit(%s, 'rescue', 'granted', %s, "
            "'expired_credential_rescued', %s::jsonb)",
            (
                worker.principal_id,
                worker.credential_id,
                json.dumps(
                    {
                        "previous_expires_at": worker.expires_at.isoformat(),
                        "new_expires_at": new_expiry.isoformat(),
                        "grace_seconds": grace_seconds,
                    }
                ),
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--grace-seconds", type=int, default=2040)
    parser.add_argument("--min-expired-seconds", type=int, default=600)
    parser.add_argument("--cooldown-seconds", type=int, default=21600)
    parser.add_argument("--max-per-day", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        from command_center.db import pool

        config = RescueConfig(**vars(args))
        # The shared control-plane pool, like infra_monitor: this tool consumes
        # the existing DB layer; it is not a new persistence engine.
        pool.open_pool(load_config())
        try:
            with pool.connection() as connection:
                connection.autocommit = True
                return run(AdminSession(connection), config)
        finally:
            pool.close_pool()
    except Exception as error:
        # Driver errors can contain connection details. Keep secrets out of logs.
        print(f"credential rescue failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
