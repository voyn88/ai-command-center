"""Service tier for the Wave-3 Marketplace (routes → **service** → repository →
db).

The routes in :mod:`command_center.api.marketplace_routes` hold no logic; they
call one function here per endpoint. This module is the only place that:

* resolves and lazily migrates the runtime db (the repository functions take an
  explicit ``db_path``);
* maps stored rows onto the :class:`command_center.api.models.MarketItem` /
  :class:`~command_center.api.models.MarketInstallLogEntry` contracts;
* **owns the install policy**: an install drives the listing ``listed →
  installed`` exactly once, delegates the materialisation to an injected
  :class:`~command_center.marketplace.installer.Installer` (never executing code
  itself), and records a real audit line (*who/when/what version*). A repeat
  install of an already-installed listing is an **idempotent no-op** — the same
  item is returned, the installer is not called again, and no duplicate log line
  is written. The repository is the structural backstop (transition allowlist +
  compare-and-set); the service is the arbiter of the idempotency policy.

Testability seam: every backing call (repository functions, ``resolve_db_path``)
is referenced through a module-level name so a test can monkeypatch it, and the
runtime db path resolves under the per-test ``AICC_DATA_DIR`` sandbox (see
``tests/conftest.py``). The installer is a parameter, so a test injects a
recording double and never touches real code execution.
"""

from __future__ import annotations

from pathlib import Path

from command_center.api import marketplace_schemas as s
from command_center.api import models
from command_center.marketplace.installer import Installer, NullInstaller
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION

# Repo root is three levels up: <root>/command_center/marketplace/service.py
ROOT = Path(__file__).resolve().parents[2]


class MarketItemNotFoundError(Exception):
    """Raised when an install/log operation names a listing that does not
    exist. Surfaced as HTTP 404."""


def _db_path() -> Path:
    """The runtime db path, migrated to the current schema if it lags. ``migrate``
    is idempotent; the version pre-check keeps the hot path a single cheap read on
    an already-current db while a brand-new sandbox db (each test) migrates once."""
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


def _item_from_row(row: dict) -> models.MarketItem:
    return models.MarketItem(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        version=row.get("version") or "",
        publisher=row.get("publisher") or "",
        description=row.get("description") or "",
        status=row["status"],
        provenance=row.get("provenance") or "",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _log_entry_from_row(row: dict) -> models.MarketInstallLogEntry:
    return models.MarketInstallLogEntry(
        id=row["id"],
        item_id=row["item_id"],
        actor=row["actor"],
        version=row.get("version") or "",
        kind=row["kind"],
        provenance=row.get("provenance") or "",
        installer=row.get("installer") or "",
        detail=row.get("detail") or "",
        metadata={str(k): str(v) for k, v in (row.get("metadata") or {}).items()},
        installed_at=row.get("installed_at"),
    )


# --------------------------------------------------------------------------
# register / list / get
# --------------------------------------------------------------------------


def register_item(payload: s.MarketItemCreate) -> models.MarketItem:
    """Register a new catalogue listing (always ``listed`` at creation).

    ``kind`` is validated at the persistence boundary; an unknown kind raises
    ``ValueError`` (mapped to HTTP 422 by the route) before any row is written."""
    row = db.create_market_item(
        _db_path(),
        name=payload.name,
        kind=payload.kind,
        version=payload.version,
        publisher=payload.publisher,
        description=payload.description,
        provenance=payload.provenance,
    )
    return _item_from_row(row)


def list_items(
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> s.MarketItemList:
    rows = db.list_market_items(
        _db_path(), kind=kind, status=status, limit=limit, offset=offset
    )
    return s.MarketItemList(
        items=[_item_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_item(item_id: str) -> models.MarketItem | None:
    row = db.get_market_item(_db_path(), item_id)
    if row is None:
        return None
    return _item_from_row(row)


# --------------------------------------------------------------------------
# install — the acceptance path (lifecycle + audit log)
# --------------------------------------------------------------------------


def install_item(
    item_id: str,
    *,
    actor: str,
    installer: Installer | None = None,
) -> models.MarketItem:
    """Install a listing once, recording who/when/what in the install log.

    Policy owned here:

    * A missing listing raises :class:`MarketItemNotFoundError` (HTTP 404).
    * An **already-installed** listing is an idempotent no-op: the current item
      is returned unchanged, the installer is *not* invoked, and **no** duplicate
      log line is written.
    * Otherwise the injected ``installer`` (default :class:`NullInstaller` — no
      code execution, no fetch) is asked to materialise the item; on success the
      listing is flipped ``listed → installed`` and its audit line is appended in
      one atomic transaction (see ``db.install_market_item``). An installer that
      raises leaves the listing ``listed`` and writes no log line.
    """
    path = _db_path()
    row = db.get_market_item(path, item_id)
    if row is None:
        raise MarketItemNotFoundError(f"market item {item_id!r} not found")
    if row["status"] == "installed":
        # Idempotent: installing an installed item changes nothing and must not
        # append a second trail line.
        return _item_from_row(row)

    used_installer: Installer = installer or NullInstaller()
    item = _item_from_row(row)
    outcome = used_installer.install(item)
    item_row, _log_row = db.install_market_item(
        path,
        item_id,
        expected_version=row["lock_version"],
        actor=actor,
        installer=getattr(used_installer, "name", used_installer.__class__.__name__),
        detail=outcome.detail,
        metadata=dict(outcome.metadata),
    )
    return _item_from_row(item_row)


def get_install_log(
    item_id: str, *, limit: int = 100, offset: int = 0
) -> s.MarketInstallLog | None:
    """The append-only install trail for one listing (newest first).

    Returns ``None`` when the listing does not exist (mapped to HTTP 404), so a
    log request never fabricates an empty history for an unknown id."""
    path = _db_path()
    if db.get_market_item(path, item_id) is None:
        return None
    rows = db.list_install_log(path, item_id, limit=limit, offset=offset)
    return s.MarketInstallLog(
        item_id=item_id,
        entries=[_log_entry_from_row(r) for r in rows],
        limit=limit,
        offset=offset,
    )
