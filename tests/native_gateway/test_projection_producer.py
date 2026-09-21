"""Producer → artifact → gateway round-trip.

The producer's output is only correct if the gateway can serve it: the
round-trip test feeds the freshly written artifact straight into
`FileProjectionSource` and asserts the resulting `SnapshotDTO` — the same
boundary every real client consumes.
"""

from __future__ import annotations

import json
from pathlib import Path

from command_center import backlog_client
from native_gateway.config import GatewaySettings
from native_gateway.projection_producer import build_projection, write_projection
from native_gateway.source import FileProjectionSource

_TASKS = [
    {
        "id": "T-1",
        "project": "alpha",
        "title": "Ship the thing",
        "status": "In Progress",
        "type": "feature",
    },
    {
        "id": "T-2",
        "project": "alpha",
        "title": "Stuck migration",
        "status": "Blocked",
        "type": "chore",
    },
    {
        "id": "T-3",
        "project": "beta",
        "title": "Finished work",
        "status": "Done",
        "type": "feature",
    },
]


def _seed_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "aicc"
    (root / "data").mkdir(parents=True)
    (root / "data/tasks.json").write_text(
        json.dumps(_TASKS, ensure_ascii=False), encoding="utf-8"
    )
    # The repository-wide conftest redirects AICC_DATA_DIR to a shared temp
    # directory; pin it to this seeded root so the producer reads exactly the
    # state built here (and local hermetic runs behave identically to CI).
    monkeypatch.setenv("AICC_DATA_DIR", str(root / "data"))
    return root


#: A `backlog_task` row as `backlog_export.fetch_rows` returns one, for the
#: tests that render a real projection instead of typing markdown by hand.
_EXPORT_ROW = {
    "task_id": "VOYN-W0-EXPORTED",
    "wave": "0",
    "priority": "P1",
    "status": "OPEN",
    "title": "exported-thing",
    "repo": "ai-command-center",
    "updated_at": None,
}


def test_build_projection_maps_tasks_and_projects(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    projection = build_projection(root)

    assert projection["projection_version"] == "1"
    assert projection["revision"].startswith("r-")
    # No runtime.db in this root: the run journal is unavailable → degraded,
    # but the task list still projects fully.
    assert projection["degraded"] is True

    tasks = {t["id"]: t for t in projection["tasks"]}
    assert tasks["T-1"]["title"] == "Ship the thing"
    assert tasks["T-1"]["blocker"] is None
    assert tasks["T-2"]["blocker"]  # Blocked lane surfaces a blocker text
    assert tasks["T-1"]["evidence"]["ci"] == "unknown"

    projects = {p["id"]: p for p in projection["projects"]}
    # read_model semantics: Blocked is not "active"; it counts as attention here.
    assert projects["alpha"]["active_tasks"] == 1
    assert projects["alpha"]["needs_attention"] == 1
    assert projects["beta"]["active_tasks"] == 0
    assert projects["beta"]["state"] == "idle"


def test_revision_is_stable_for_identical_state(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    first = build_projection(root)
    second = build_projection(root)
    assert first["revision"] == second["revision"]


def test_round_trip_through_gateway_source(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    out = tmp_path / "artifact/projection.json"
    write_projection(root, out)

    settings = GatewaySettings(projection_path=out, token_file=tmp_path / "unused")
    snapshot = FileProjectionSource(settings).load().snapshot

    assert snapshot.schemaVersion == "1.0"
    # Producer-declared degraded (no run journal) must reach the client.
    assert snapshot.freshness.value == "degraded"
    assert {t.id for t in snapshot.tasks} == {"T-1", "T-2", "T-3"}
    assert snapshot.tasks[1].blocker is not None
    assert {p.id for p in snapshot.projects} == {"alpha", "beta"}
    assert snapshot.revision == json.loads(out.read_text())["revision"]


def test_artifact_survives_gateway_redaction_cleanly(tmp_path, monkeypatch):
    """A normal producer artifact must not trip the redaction boundary."""
    root = _seed_root(tmp_path, monkeypatch)
    out = tmp_path / "projection.json"
    write_projection(root, out)
    settings = GatewaySettings(projection_path=out, token_file=tmp_path / "unused")
    snapshot = FileProjectionSource(settings).load().snapshot
    assert snapshot.tasks[0].title == "Ship the thing"  # not [REDACTED]


def test_dialogs_project_summaries_never_content(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    (root / "data/chats.json").write_text(
        json.dumps(
            [
                {
                    "id": "c-1",
                    "title": "Обсуждение дизайна",
                    "updated_at": "2026-08-26T01:00:00",
                    "messages": [
                        {"role": "user", "content": "секретный вопрос про password"},
                        {"role": "assistant", "content": "ответ"},
                    ],
                },
                {
                    "id": "c-2",
                    "title": "Недельный бриф",
                    "updated_at": "2026-08-26T02:00:00",
                    "messages": [],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    projection = build_projection(root)
    dialogs = projection["dialogs"]
    assert [d["id"] for d in dialogs] == ["c-2", "c-1"]  # newest first
    assert dialogs[1]["message_count"] == 2
    assert dialogs[1]["last_summary"] is None
    # Raw message content must never appear anywhere in the projection.
    assert "секретный" not in json.dumps(projection, ensure_ascii=False)


def test_rich_execution_status_wins_and_maps_to_state(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    backlog = tmp_path / "backlog.md"
    backlog.write_text(
        "- VOYN_RECOMMENDATION | ts=2026-08-26T00:00:00Z | status=PO-Approved | "
        "issue_id=VOYN-W0-RICH-A | current_wave=W0 | proposed_wave=W0 | priority=P1 | "
        "owner=x | effect=high | effort=S | acceptance=accept:a | "
        "task=do_thing | evidence=none | file_scope=NONE | parallel_domain=ops\n"
        "- **VOYN-W0-RICH-A** | Wave 0 | IN_PROGRESS | P1 | X | `do-thing` | t\n"
        "- **VOYN-W0-RICH-B** | Wave 0 | DONE | P1 | X | `finished-thing` | t\n"
        "- **VOYN-W0-RICH-C** | Wave 0 | DEFER_TO_USER | P1 | X | `needs-owner` | t\n",
        encoding="utf-8",
    )
    projection = build_projection(root, backlog_path=backlog)
    tasks = {t["id"]: t for t in projection["tasks"]}
    # Execution status from the rich line overrides the planning status.
    assert tasks["VOYN-W0-RICH-A"]["state"] == "in_progress"
    assert tasks["VOYN-W0-RICH-B"]["state"] == "done"
    assert tasks["VOYN-W0-RICH-C"]["state"] == "deferred"
    assert tasks["VOYN-W0-RICH-C"]["blocker"] == "Ждёт вашего решения"
    # read_model lane placement follows: done leaves active, deferred needs attention.
    backlog_project = next(p for p in projection["projects"] if p["id"] == "backlog")
    assert backlog_project["needs_attention"] >= 1
    ops_project = next(p for p in projection["projects"] if p["id"] == "ops")
    assert ops_project["active_tasks"] >= 1


def test_goal_is_the_earliest_unfinished_wave(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    backlog = tmp_path / "backlog.md"
    backlog.write_text(
        "- **VOYN-W0-G-A** | Wave 0 | DONE | P1 | X | `a` | t\n"
        "- **VOYN-W0-G-B** | Wave 0 | DONE | P1 | X | `b` | t\n"
        "- **VOYN-W1-G-C** | Wave 1 | IN_PROGRESS | P1 | X | `c` | t\n"
        "- **VOYN-W1-G-D** | Wave 1 | OPEN | P1 | X | `d` | t\n"
        "- **VOYN-W1-G-E** | Wave 1 | READY_TO_REVIEW | P1 | X | `e` | t\n",
        encoding="utf-8",
    )
    projection = build_projection(root, backlog_path=backlog)
    goal = projection["goal"]
    # Wave 0 is fully done -> the goal is Wave 1 with its real counts.
    assert goal == {
        "title": "Волна 1",
        "done": 0,
        "total": 3,
        "in_progress": 1,
        "review": 1,
    }


def test_goal_survives_round_trip_to_snapshot(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    backlog = tmp_path / "backlog.md"
    backlog.write_text(
        "- **VOYN-W0-RT-A** | Wave 0 | DONE | P1 | X | `a` | t\n"
        "- **VOYN-W0-RT-B** | Wave 0 | OPEN | P1 | X | `b` | t\n",
        encoding="utf-8",
    )
    out = tmp_path / "projection.json"
    write_projection(root, out, backlog_path=backlog)
    settings = GatewaySettings(projection_path=out, token_file=tmp_path / "unused")
    snapshot = FileProjectionSource(settings).load().snapshot
    assert snapshot.goal is not None
    assert snapshot.goal.title == "Волна 0"
    assert (snapshot.goal.done, snapshot.goal.total) == (1, 2)


def test_every_execution_status_has_a_lane(tmp_path, monkeypatch):
    """`_RICH_STATE` is subscripted, not `.get`-ed, so a status with no entry
    is a KeyError that takes the whole projection build down — every card, not
    just the unmapped one.

    That was survivable while the vocabulary only grew by hand; since
    `backlog-export` renders this surface straight from `backlog_task`, a
    status the store's CHECK constraint allows can arrive here the moment a
    row carries it. Pins totality over the reader's vocabulary so the gap
    shows up as this test failing rather than as a dark board."""
    from native_gateway.projection_producer import _RICH_STATE

    assert set(_RICH_STATE) == set(backlog_client.RICH_STATUSES) | {"UNKNOWN"}
    # Every lane must also be one the client actually renders.
    states = {state for state, _ in _RICH_STATE.values()}
    assert states <= {"backlog", "next", "in_progress", "review", "done", "deferred"}


def test_an_exported_projection_drives_the_board_and_the_goal(tmp_path, monkeypatch):
    """End to end over the bridge, against the file `backlog-export` really
    writes rather than a hand-typed fixture.

    This is what BO-S4 is for. The console's board used to read an authored
    markdown file that stopped being true weeks earlier; now it reads a
    rendering of the canonical store. Every hop has to survive: store row ->
    `render_projection` -> `parse_rich_records` -> lane. If the exporter
    rendered the bare wave column instead of the wave TEXT, or coarsened
    execution status the way section 0B must, this still produces a valid
    projection — just one with an empty goal card and every task in Backlog.
    So both are asserted here, not only that the call succeeded."""
    from datetime import UTC, datetime

    from command_center.db import backlog_export

    root = _seed_root(tmp_path, monkeypatch)
    rows = [
        {
            "task_id": "VOYN-W0-BO-S1",
            "wave": "0",
            "priority": "P0",
            "status": "DONE",
            "title": "structured-store",
            "repo": "ai-command-center",
            "updated_at": datetime(2026, 9, 9, 6, 0, tzinfo=UTC),
        },
        {**_EXPORT_ROW, "task_id": "VOYN-W0-BO-S4", "status": "IN_PROGRESS"},
        {**_EXPORT_ROW, "task_id": "VOYN-W0-BO-S5", "status": "READY_TO_REVIEW"},
        {**_EXPORT_ROW, "task_id": "VOYN-W0-BO-S6", "status": "DEFER_TO_USER"},
    ]
    backlog = tmp_path / "backlog.md"
    backlog.write_text(
        backlog_export.render_projection(
            rows, generated_at=datetime(2026, 9, 9, 6, 30, tzinfo=UTC)
        ),
        encoding="utf-8",
    )

    projection = build_projection(root, backlog_path=backlog)
    tasks = {t["id"]: t for t in projection["tasks"]}
    assert tasks["VOYN-W0-BO-S1"]["state"] == "done"
    assert tasks["VOYN-W0-BO-S4"]["state"] == "in_progress"
    assert tasks["VOYN-W0-BO-S5"]["state"] == "review"
    assert tasks["VOYN-W0-BO-S6"]["state"] == "deferred"
    assert tasks["VOYN-W0-BO-S6"]["blocker"] == "Ждёт вашего решения"
    # Both surfaces name the card from the same `backlog_task.title` column
    # (which IS the authored backtick slug), so the board cannot show one task
    # under two names depending on which record it was built from. The 0B
    # record supplies it here -- the rich loop `setdefault`s -- and 0B's
    # humanizer leaves `-` alone where the rich one would space it.
    assert tasks["VOYN-W0-BO-S1"]["title"] == "Structured-store"
    # ...and the goal card counts the real wave, which only works because the
    # exporter renders `wave` as the "Wave <n>" text `_wave_goal` matches.
    assert projection["goal"] == {
        "title": "Волна 0",
        "done": 1,
        "total": 4,
        "in_progress": 1,
        "review": 1,
    }
