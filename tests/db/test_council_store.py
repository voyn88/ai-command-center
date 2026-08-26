"""Slice 9: what is specific to the Council family, and only that.

The shared machinery now carries what used to be written per slice. Every
declaration in `command_center/db/*_store.py` is enrolled automatically by
`tests/db/test_mirror_contract.py` — columns against the accepted schema and
the live SQLite one, the declared key and references against the DDL, a round
trip that reconciles, a child refused without its parent, timestamps in the
authority's format, `jsonb` compared by value, identity preserved and
resyncable. `tests/db/test_stored_reader_fitness.py` requires a stored-shape
reader wherever a public reader decodes, and requires the reconciliation's
docstring to name it.

So this file is short on purpose. What it holds is what no shared contract can
know: **which authority write paths mirror, in what order, and whether a lost
one is visible.**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.db.council_store import (
    PostgresCouncilDecisionMirror,
    PostgresCouncilEventMirror,
    PostgresCouncilVoteMirror,
    PostgresMotionMirror,
    decision_divergence,
    event_divergence,
    motion_divergence,
    vote_divergence,
)
from command_center.runtime.db import council as council_db
from tests.db.mirror_probe import each_lost_write_is_noticed

TALLY = {"approve": 2, "reject": 0}
ROLES = [{"role": "architect", "choice": "approve"}]


def _mirrors(factory):
    return {
        "motion": PostgresMotionMirror(connection_factory=factory),
        "vote": PostgresCouncilVoteMirror(connection_factory=factory),
        "decision": PostgresCouncilDecisionMirror(connection_factory=factory),
        "event": PostgresCouncilEventMirror(connection_factory=factory),
    }


def _patch_all(monkeypatch, factory) -> None:
    from command_center.db import council_store

    for name, mirror in (
        ("PostgresMotionMirror", PostgresMotionMirror),
        ("PostgresCouncilVoteMirror", PostgresCouncilVoteMirror),
        ("PostgresCouncilDecisionMirror", PostgresCouncilDecisionMirror),
        ("PostgresCouncilEventMirror", PostgresCouncilEventMirror),
    ):
        # The real class is captured here, before the patch — reading it back
        # through the module inside the lambda would resolve to the lambda.
        # Slice 4's acceptance warned about that shape and slice 8 walked into
        # it anyway.
        monkeypatch.setattr(
            council_store, name, lambda mirror=mirror: mirror(connection_factory=factory)
        )


def _lifecycle(db_path: Path) -> str:
    """Open a motion, vote on it, decide it — the family's whole write surface
    except withdrawal, which is the other terminal move."""
    motion = council_db.create_motion(db_path, title="mirror the council", proposed_by="ops")
    council_db.cast_vote(
        db_path, motion_id=motion["id"], voter_id="architect", role="architect", choice="yes"
    )
    council_db.record_decision(
        db_path,
        motion_id=motion["id"],
        expected_version=motion["version"],
        outcome="approved",
        tally=TALLY,
        roles=ROLES,
        rationale="quorum met",
        quorum=motion["quorum"],
    )
    return motion["id"]


def test_the_family_reconciles_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Staged, because two paths rewrite the motion row.

    `record_decision` moves the motion to `decided` and `withdraw_motion` to
    `withdrawn`; either would cover a lost create in an end-state check. Slice 5
    needed three rounds to state that truthfully, so it is stated here by
    construction: reconcile all four tables after each authority call.
    """
    _patch_all(monkeypatch, pg_connection_factory)
    mirrors = _mirrors(pg_connection_factory)
    db_path = tmp_path / "runtime.db"
    council_db.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        assert motion_divergence(council_db.list_motions(db_path), mirrors["motion"]) == [], stage
        assert vote_divergence(
            council_db.list_votes(db_path, motion_id), mirrors["vote"]
        ) == [], stage
        assert decision_divergence(
            council_db.list_decisions_stored(db_path), mirrors["decision"]
        ) == [], stage
        assert event_divergence(
            council_db.list_events_stored(db_path), mirrors["event"]
        ) == [], stage

    motion = council_db.create_motion(db_path, title="mirror the council", proposed_by="ops")
    motion_id = motion["id"]
    reconciled("motion opened")

    council_db.cast_vote(
        db_path, motion_id=motion_id, voter_id="architect", role="architect", choice="yes"
    )
    reconciled("vote cast")

    council_db.record_decision(
        db_path,
        motion_id=motion_id,
        expected_version=motion["version"],
        outcome="approved",
        tally=TALLY,
        roles=ROLES,
        rationale="quorum met",
        quorum=motion["quorum"],
    )
    reconciled("decision recorded")

    # The withdrawal path on a second motion, since `decided` is terminal.
    other = council_db.create_motion(db_path, title="second", proposed_by="ops")
    reconciled("second motion opened")
    council_db.withdraw_motion(db_path, other["id"], expected_version=other["version"])
    reconciled("second motion withdrawn")


def test_every_lost_mirror_write_is_visible_to_reconciliation(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Seven writes across four tables, each failed in turn.

    The probe counts the writes from the current code on every run, so a
    scenario that grows a write later cannot leave this testing a stale
    position — the mistake slice 5 made when it replayed a perturbation by
    ordinal.

    It corrected this test on the first run: I wrote `== 6`, because I counted
    two writes for `create_motion`, two for `cast_vote` and three for
    `record_decision` and then mis-added. Seven is right, and the seventh is
    `record_decision`'s mirror of the *motion* — the path also moves it to
    `decided`, so the parent row changes. That is the number the assertion now
    carries, measured rather than believed.
    """
    from command_center.db import council_store

    mirrors = _mirrors(pg_connection_factory)
    state: dict[str, object] = {}

    def scenario() -> None:
        for table in ("council_event", "council_decision", "council_vote", "motion"):
            with pg_connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table}")
        db_path = tmp_path / f"runtime-{len(state)}.db"
        council_db.db.migrate(db_path)
        state["db"] = db_path
        state["motion"] = _lifecycle(db_path)

    def noticed() -> bool:
        db_path = state["db"]
        return bool(
            motion_divergence(council_db.list_motions(db_path), mirrors["motion"])
            or vote_divergence(
                council_db.list_votes(db_path, str(state["motion"])), mirrors["vote"]
            )
            or decision_divergence(council_db.list_decisions_stored(db_path), mirrors["decision"])
            or event_divergence(council_db.list_events_stored(db_path), mirrors["event"])
        )

    results = each_lost_write_is_noticed(
        monkeypatch,
        targets=(
            (
                council_store,
                ("PostgresMotionMirror",),
                lambda: PostgresMotionMirror(connection_factory=pg_connection_factory),
            ),
            (
                council_store,
                ("PostgresCouncilVoteMirror",),
                lambda: PostgresCouncilVoteMirror(connection_factory=pg_connection_factory),
            ),
            (
                council_store,
                ("PostgresCouncilDecisionMirror",),
                lambda: PostgresCouncilDecisionMirror(connection_factory=pg_connection_factory),
            ),
            (
                council_store,
                ("PostgresCouncilEventMirror",),
                lambda: PostgresCouncilEventMirror(connection_factory=pg_connection_factory),
            ),
        ),
        scenario=scenario,
        noticed=noticed,
    )

    assert len(results) == 7, [result.target for result in results]
    missed = [result for result in results if not result.noticed]
    assert not missed, f"lost writes nothing noticed: {missed}"


def test_reconciling_decisions_and_events_against_the_decoding_readers_is_not_clean(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Two decoding readers in one family, pinned together.

    `list_decisions` drops both JSON columns and `list_events` drops one. An
    operator reaching for either gets 100% divergence and a failure that looks
    like a broken mirror. The fitness test requires the stored readers to exist;
    this shows what happens without them.
    """
    _patch_all(monkeypatch, pg_connection_factory)
    mirrors = _mirrors(pg_connection_factory)
    db_path = tmp_path / "runtime.db"
    council_db.db.migrate(db_path)
    motion_id = _lifecycle(db_path)

    decoded_decisions = council_db.list_decisions(db_path)
    assert "tally_json" not in decoded_decisions[0]  # the premise
    assert decision_divergence(decoded_decisions, mirrors["decision"]) != []

    decoded_events = council_db.list_events(db_path, motion_id)
    assert "metadata_json" not in decoded_events[0]
    assert event_divergence(decoded_events, mirrors["event"]) != []

    # And clean against the readers that exist for this.
    assert decision_divergence(council_db.list_decisions_stored(db_path), mirrors["decision"]) == []
    assert event_divergence(council_db.list_events_stored(db_path), mirrors["event"]) == []


def test_the_two_json_columns_disagree_about_canonical_form(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Why this family is the clearest argument for comparing `jsonb` by value.

    `record_decision` writes `tally_json` with `sort_keys=True` and `roles_json`
    without. A text comparison would therefore survive on one column and fail on
    its neighbour — in the same row, from the same writer.
    """
    _patch_all(monkeypatch, pg_connection_factory)
    mirrors = _mirrors(pg_connection_factory)
    db_path = tmp_path / "runtime.db"
    council_db.db.migrate(db_path)
    _lifecycle(db_path)

    stored = council_db.list_decisions_stored(db_path)[0]
    import json

    assert stored["tally_json"] == json.dumps(TALLY, ensure_ascii=False, sort_keys=True)
    assert stored["roles_json"] == json.dumps(ROLES, ensure_ascii=False)
    assert decision_divergence([stored], mirrors["decision"]) == []


def test_a_mirror_failure_cannot_break_any_authoritative_write(tmp_path, monkeypatch) -> None:
    from command_center.db import council_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

    for name in (
        "PostgresMotionMirror",
        "PostgresCouncilVoteMirror",
        "PostgresCouncilDecisionMirror",
        "PostgresCouncilEventMirror",
    ):
        monkeypatch.setattr(council_store, name, lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    council_db.db.migrate(db_path)
    motion_id = _lifecycle(db_path)

    assert council_db.get_motion(db_path, motion_id)["status"] == "decided"
    assert len(council_db.list_votes(db_path, motion_id)) == 1
    assert council_db.get_decision(db_path, motion_id)["outcome"] == "approved"
    assert len(council_db.list_events_stored(db_path)) == 3


def test_a_double_vote_is_still_refused_by_the_authority(tmp_path, monkeypatch) -> None:
    """`UNIQUE (motion_id, voter_id)` is the authority's rule, and the mirror
    must not change when it fires: a refused vote is not a mirrored one."""
    from command_center.db import council_store

    seen: list[str] = []

    class Recording:
        def upsert(self, record: dict) -> None:
            seen.append(record.get("voter_id") or record.get("id") or "?")

    for name in (
        "PostgresMotionMirror",
        "PostgresCouncilVoteMirror",
        "PostgresCouncilDecisionMirror",
        "PostgresCouncilEventMirror",
    ):
        monkeypatch.setattr(council_store, name, lambda: Recording())

    db_path = tmp_path / "runtime.db"
    council_db.db.migrate(db_path)
    motion = council_db.create_motion(db_path, title="t", proposed_by="ops")
    council_db.cast_vote(
        db_path, motion_id=motion["id"], voter_id="architect", role="architect", choice="yes"
    )
    before = len(seen)

    with pytest.raises(council_db.DoubleVoteError):
        council_db.cast_vote(
            db_path, motion_id=motion["id"], voter_id="architect", role="architect", choice="no"
        )

    assert len(seen) == before, "a refused vote must not reach the mirror"
