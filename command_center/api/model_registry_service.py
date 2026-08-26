"""Service tier for the Wave-3 model-registry surface (routes → **service** →
repository → db).

The routes in :mod:`command_center.api.model_registry_routes` hold no logic; they
call one function here per endpoint. This module is the only place that:

* resolves and lazily migrates the runtime db (the repository functions take an
  explicit ``db_path``);
* maps stored rows onto the :mod:`command_center.api.models` contract;
* drives a local model's download lifecycle through the **injected** downloader
  (available → downloading → progress ticks → installed / error), persisting each
  step as a real governance-log event — no network in tests, real lifecycle;
* enforces the **sensitive-data guard**: a context marked sensitive is never
  routed to an external model (:func:`command_center.models_registry.policy`);
* records every model action (register, download-request, download-progress,
  assign, use, status-change) in the governance log so a model's history is fully
  traceable (the VOYN-W3 acceptance);
* publishes domain events onto the in-process bus after a write commits.

Testability seam: the downloader factory (``_downloader``) and every backing call
(repository functions, ``resolve_db_path``) is referenced through a module-level
name so a test can monkeypatch it. The runtime db path resolves under the
per-test ``AICC_DATA_DIR`` sandbox.
"""

from __future__ import annotations

from pathlib import Path

from command_center.api import model_registry_schemas as s
from command_center.api import models
from command_center.events import (
    ModelAssigned,
    ModelRegistered,
    ModelStatusChanged,
    default_bus,
)
from command_center.models_registry import (
    Downloader,
    StubDownloader,
    assert_routing_allowed,
    auto_select,
)
from command_center.models_registry.downloader import DownloadFailed
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION

# Repo root is three levels up: <root>/command_center/api/model_registry_service.py
ROOT = Path(__file__).resolve().parents[2]

# Downloader seam (module-level so a test injects a stub / failing downloader).
# Defaults to the network-free stub — production wires a real fetcher here.
_downloader: Downloader = StubDownloader()


class ModelNotDownloadableError(Exception):
    """Raised when a download is requested for an external model. External models
    are hosted, not fetched — there is nothing to download — so the request is a
    client error (409) rather than a no-op that silently succeeds."""


def _db_path() -> Path:
    """The runtime db path, migrated to the current schema if it lags (the same
    lazy-migrate pattern the Wave-1/2 services use)."""
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


# --------------------------------------------------------------------------
# Row -> contract-model mapping
# --------------------------------------------------------------------------


def _entry_from_row(row: dict) -> models.ModelEntry:
    return models.ModelEntry(
        id=row["id"],
        name=row.get("name") or "",
        kind=row["kind"],
        provider=row.get("provider"),
        status=row["status"],
        cost=row.get("cost"),
        quality=row.get("quality"),
        latency_ms=row.get("latency_ms"),
        provenance=row.get("provenance"),
        download_progress=int(row.get("download_progress") or 0),
        created_at=row.get("created_at"),
    )


def _event_from_row(row: dict) -> models.ModelEvent:
    return models.ModelEvent(
        seq=int(row["seq"]),
        model_id=row["model_id"],
        action=row["action"],
        actor=row.get("actor"),
        target_ref=row.get("target_ref"),
        provenance=row.get("provenance"),
        metadata=row.get("metadata") or {},
        created_at=row.get("created_at"),
    )


# --------------------------------------------------------------------------
# Register / list / get
# --------------------------------------------------------------------------


def register_model(payload: s.RegisterModelRequest) -> models.ModelEntry:
    """Register a model and emit :class:`ModelRegistered`. The ``register``
    governance event is written atomically with the row by the repository."""
    row = db.create_model_entry(
        _db_path(),
        name=payload.name,
        kind=payload.kind,
        provider=payload.provider,
        cost=payload.cost,
        quality=payload.quality,
        latency_ms=payload.latency_ms,
        provenance=payload.provenance,
        actor=payload.actor,
    )
    default_bus().publish(
        ModelRegistered(
            model_id=row["id"], kind=row["kind"], provider=row.get("provider")
        ),
        raise_errors=False,
    )
    return _entry_from_row(row)


def list_models(
    *, kind: str | None = None, status: str | None = None,
    limit: int = 100, offset: int = 0,
) -> s.ModelList:
    rows = db.list_model_entries(
        _db_path(), kind=kind, status=status, limit=limit, offset=offset
    )
    return s.ModelList(
        models=[_entry_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_model(model_id: str) -> models.ModelEntry | None:
    row = db.get_model_entry(_db_path(), model_id)
    return _entry_from_row(row) if row is not None else None


# --------------------------------------------------------------------------
# Download lifecycle (drives the injected downloader)
# --------------------------------------------------------------------------


def download_model(
    model_id: str, payload: s.DownloadModelRequest
) -> s.DownloadModelResult | None:
    """Run a local model's download lifecycle through the injected downloader.

    Moves the model available → downloading (a ``download-request`` event),
    persists every progress tick the downloader yields (each a
    ``download-progress`` event and a real progress update), then installs it on
    success or marks it ``error`` on failure. Returns ``None`` if the model does
    not exist; raises :class:`ModelNotDownloadableError` for an external model."""
    path = _db_path()
    row = db.get_model_entry(path, model_id)
    if row is None:
        return None
    if row["kind"] != "local":
        raise ModelNotDownloadableError(
            f"model {model_id!r} is external; nothing to download"
        )
    provenance = payload.provenance or row.get("provenance")
    current = db.set_model_status(
        path, model_id, expected_version=row["version"], status="downloading",
        download_progress=0, action="download-request", actor=payload.actor,
        provenance=provenance,
    )
    percents: list[int] = []
    try:
        for tick in _downloader.fetch(model_id=model_id, provenance=provenance):
            current = db.update_download_progress(
                path, model_id, expected_version=current["version"],
                progress=tick.percent, actor=payload.actor,
            )
            percents.append(tick.percent)
    except DownloadFailed:
        errored = db.set_model_status(
            path, model_id, expected_version=current["version"], status="error",
            action="status-change", actor=payload.actor,
        )
        default_bus().publish(
            ModelStatusChanged(model_id=model_id, status="error"),
            raise_errors=False,
        )
        return s.DownloadModelResult(
            model=_entry_from_row(errored), progress=percents
        )
    installed = db.set_model_status(
        path, model_id, expected_version=current["version"], status="installed",
        download_progress=100, action="status-change", actor=payload.actor,
    )
    default_bus().publish(
        ModelStatusChanged(model_id=model_id, status="installed"),
        raise_errors=False,
    )
    return s.DownloadModelResult(model=_entry_from_row(installed), progress=percents)


# --------------------------------------------------------------------------
# Assignment + use (the governance-logged routing actions)
# --------------------------------------------------------------------------


def assign_model(
    model_id: str, payload: s.AssignModelRequest
) -> s.AssignModelResponse | None:
    """Assign a model to a task/agent and record it in the governance log.

    Enforces the sensitive guard: a context marked ``sensitive`` may not be routed
    to an external model (raises :class:`SensitiveModelRoutingError`). Returns
    ``None`` if the model does not exist."""
    path = _db_path()
    row = db.get_model_entry(path, model_id)
    if row is None:
        return None
    # Guard first — a rejected assignment must not reach the governance log.
    assert_routing_allowed(row, sensitive=payload.sensitive)
    db.append_model_event(
        path, model_id, action="assign", actor=payload.actor,
        target_ref=payload.target_ref, provenance=payload.provenance,
        metadata={"sensitive": payload.sensitive},
    )
    default_bus().publish(
        ModelAssigned(model_id=model_id, target_ref=payload.target_ref),
        raise_errors=False,
    )
    updated = db.get_model_entry(path, model_id)
    return s.AssignModelResponse(
        model=_entry_from_row(updated), target_ref=payload.target_ref
    )


def record_use(
    model_id: str, *, target_ref: str, sensitive: bool = False,
    actor: str | None = None, provenance: str | None = None,
) -> models.ModelEvent | None:
    """Record that a model was actually used against ``target_ref``. Re-applies
    the sensitive guard (a use is a fresh routing decision) and appends a ``use``
    governance event so the model's history captures invocations, not just
    assignments. Returns ``None`` if the model does not exist."""
    path = _db_path()
    row = db.get_model_entry(path, model_id)
    if row is None:
        return None
    assert_routing_allowed(row, sensitive=sensitive)
    event = db.append_model_event(
        path, model_id, action="use", actor=actor, target_ref=target_ref,
        provenance=provenance, metadata={"sensitive": sensitive},
    )
    return _event_from_row(event)


def auto_select_model(
    *, sensitive: bool = False, limit: int = 500
) -> models.ModelEntry | None:
    """Pick the best usable model, preferring local (cost). A thin service wrapper
    over :func:`command_center.models_registry.policy.auto_select` that reads the
    catalog and applies the sensitive filter. Returns ``None`` if none qualify."""
    rows = db.list_model_entries(_db_path(), limit=limit)
    chosen = auto_select(rows, sensitive=sensitive)
    return _entry_from_row(dict(chosen)) if chosen is not None else None


# --------------------------------------------------------------------------
# History (the traceable governance log)
# --------------------------------------------------------------------------


def get_history(model_id: str) -> s.ModelHistory | None:
    """The full, ordered governance log for a model. Returns ``None`` if the model
    does not exist (so the route can 404) rather than an empty history that would
    read as 'exists, no actions'."""
    path = _db_path()
    if db.get_model_entry(path, model_id) is None:
        return None
    rows = db.list_model_events(path, model_id)
    return s.ModelHistory(
        model_id=model_id, events=[_event_from_row(r) for r in rows]
    )
