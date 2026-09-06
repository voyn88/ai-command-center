"""Reads real ledger rows into `dispatch.rating.LedgerEntry` values
(VOYN-W0-AICC-AGENT-MARKETPLACE).

`ledger_feed.ledger_entry_from_run` is a pure row-in/value-out adapter; its
own docstring named the join it deliberately did not do as "a separate,
later seam": pulling real `run` / `completion` / `run_provenance` rows, plus
per-run cost from `run_event`, and feeding each through the adapter. This
module is that seam — the one impure module in this package, the same way
`dispatch.service` is the impure orchestration layer in front of the pure
`dispatch.policy` engine.

Only *finished* runs are attempts worth rating: `runtime_db.TERMINAL_STATES`
excludes `QUEUED`/`RUNNING`/`PREPARED`, so a run still in flight is not yet
counted as an attempt that failed to land — it has not failed anything yet.

Still not wired into a live selection decision (`dispatch.policy`,
`dispatch.service`): `routing_weight.routing_weights` is ready for a real
rating feed, but turning it on inside `plan_dispatch`'s executor selection is
a separate change — one that touches live dispatch behavior and deserves its
own dedicated pass, not a rider on the module that first makes the feed
real.
"""

from __future__ import annotations

from pathlib import Path

from command_center import task_pipeline
from command_center.dispatch.ledger_feed import ledger_entry_from_run
from command_center.dispatch.rating import LedgerEntry
from command_center.runtime import db as runtime_db


def list_ledger_entries(
    db_path: Path, *, limit: int | None = None
) -> list[LedgerEntry]:
    """Every finished run, turned into a `LedgerEntry` from its real
    `completion` / `run_provenance` / cost data.

    Three batch reads (never one query per run — the same N+1 discipline
    `runtime.db.get_completions_for_runs` exists for, audit H5) joined by
    `run_id` in Python and handed one at a time to `ledger_entry_from_run`.
    `limit`, like `runtime_db.list_runs`, bounds how many of the most
    recently created finished runs are read; `None` reads all of them.
    """
    runs = runtime_db.list_runs(
        db_path, states=runtime_db.TERMINAL_STATES, limit=limit
    )
    run_ids = [run["id"] for run in runs]
    completions = runtime_db.get_completions_for_runs(db_path, run_ids)
    provenance = runtime_db.get_run_provenance_for_runs(db_path, run_ids)
    costs = task_pipeline.costs_usd_for_runs(db_path, run_ids)

    return [
        ledger_entry_from_run(
            run,
            completion=completions.get(run["id"]),
            provenance=provenance.get(run["id"]),
            cost_usd=costs.get(run["id"], 0.0),
        )
        for run in runs
    ]
