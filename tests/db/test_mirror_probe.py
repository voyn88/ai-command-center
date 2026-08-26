"""The lost-write probe, applied to a family that already passed review.

`tests/db/mirror_probe.py` exists because two slices claimed per-write coverage
they did not have. Its first job is to prove the claim for a family independent
review already accepted — the networking pair from slice 5 — so the helper is
measured against a known-good answer before it is trusted on a new table.
"""

from __future__ import annotations

import pytest

from command_center.db.networking_store import (
    PostgresContactMirror,
    PostgresMessageMirror,
    contact_divergence,
    message_divergence,
)
from command_center.runtime.db import networking as net_db
from tests.db.mirror_probe import each_lost_write_is_noticed


def test_every_lost_mirror_write_is_visible_to_reconciliation(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Five writes, five runs, each failing exactly one — all five reported.

    This is the property slice 5 needed three rounds to state truthfully, and
    the reason it is stated per write rather than at the end: an end-state
    reconciliation cannot see an intermediate write that a later whole-row
    write covers. Reconciling after every write is what makes the claim true;
    this probe is what proves it stayed true.
    """
    from command_center.db import networking_store

    db_path = tmp_path / "runtime.db"
    net_db.db.migrate(db_path)

    contacts = PostgresContactMirror(connection_factory=pg_connection_factory)
    messages = PostgresMessageMirror(connection_factory=pg_connection_factory)

    state: dict[str, str] = {}

    def scenario() -> None:
        # A fresh authority per run, so the runs cannot contaminate each other,
        # and a fresh mirror for the same reason.
        for table in ("message", "contact"):
            with pg_connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table}")
        db = tmp_path / f"runtime-{len(state)}.db"
        net_db.db.migrate(db)
        state["db"] = str(db)

        quiet = net_db.create_contact(db, display_name="quiet")
        net_db.create_message(db, contact_id=quiet["id"], body="only")
        talked = net_db.create_contact(db, display_name="talked")
        net_db.update_contact_fields(
            db, talked["id"], expected_version=talked["version"], fields={"note": "n"}
        )
        net_db.create_message(db, contact_id=talked["id"], body="hello")

    def noticed() -> bool:
        db = state["db"]
        from pathlib import Path

        return bool(
            contact_divergence(net_db.list_contacts(Path(db)), contacts)
            or message_divergence(net_db.list_messages(Path(db)), messages)
        )

    results = each_lost_write_is_noticed(
        monkeypatch,
        targets=(
            (
                networking_store,
                ("PostgresContactMirror",),
                lambda: PostgresContactMirror(connection_factory=pg_connection_factory),
            ),
            (
                networking_store,
                ("PostgresMessageMirror",),
                lambda: PostgresMessageMirror(connection_factory=pg_connection_factory),
            ),
        ),
        scenario=scenario,
        noticed=noticed,
    )

    assert len(results) == 5, [result.target for result in results]
    missed = [result for result in results if not result.noticed]
    assert not missed, f"lost writes nothing noticed: {missed}"


def test_the_probe_refuses_a_scenario_that_mirrors_nothing(monkeypatch) -> None:
    """A probe that silently measures zero writes would report "all caught" for
    a scenario that never mirrored anything — the shape of a vacuous test."""
    with pytest.raises(AssertionError, match="no mirror writes"):
        each_lost_write_is_noticed(
            monkeypatch, targets=(), scenario=lambda: None, noticed=lambda: False
        )
