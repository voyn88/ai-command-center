"""Wave-3 Marketplace table-family (VOYN-W3-MARKET): the persistence tier behind
the module/add-on catalogue (routes → service → repository → db).

This module owns two additive tables, wholly separate from every other family in
:mod:`command_center.runtime.db`:

* ``market_item`` — one mutable current-state row per catalogue listing, guarded
  by a ``lock_version`` compare-and-set column and an explicit status allowlist
  (:data:`MARKET_ITEM_TRANSITIONS`, ``listed → installed``; ``installed`` is
  terminal). The package ``version``/``publisher``/``provenance`` are plain
  descriptive columns.
* ``market_install_log`` — append-only. One immutable row per install recording
  *who* (``actor``), *when* (``installed_at``) and *what version* of *which*
  listing was installed, plus the ``installer`` implementation that performed it.
  This is the acceptance artefact of the install path — a real, queryable record,
  never a placeholder.

Every write goes through the shared ``connect()``/``transaction()`` primitives
(WAL, ``BEGIN IMMEDIATE`` write lock, per-row ``version`` compare-and-set), so
the single-writer discipline the rest of ``runtime.db`` follows holds here too:
this module is the *only* writer of both tables.

Kinds/statuses are stored as their stable string *values* (never a Python enum's
member name) so a column round-trips to exactly the Literal the API contract
(``api/models.py``) declares — the enum-name lesson.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes keep intercepting internal calls exactly as they
do for the other table-family modules.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import command_center.runtime.db as db  # facade (late-bound; see docstring)

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Allowlists (mirror ``api.models`` Literals; validated at the boundary)
# --------------------------------------------------------------------------

#: What a listing *is* (mirrors ``api.models.MarketItemKind``). Validated at the
#: persistence boundary so a malformed kind can never reach a stored row.
MARKET_ITEM_KINDS: frozenset[str] = frozenset({"module", "domain_pack", "plugin"})

#: A listing's lifecycle (mirrors ``api.models.MarketItemStatus``). ``installed``
#: is terminal — this baseline wave has no un-install.
MARKET_ITEM_STATUSES: frozenset[str] = frozenset({"listed", "installed"})

#: The only allowed status edge: a listed item may be installed once. Everything
#: else (a re-install, any move out of ``installed``) is refused here; the
#: *service* additionally makes a repeat install an idempotent no-op rather than
#: an error before it ever reaches this edge.
MARKET_ITEM_TRANSITIONS: dict[str, frozenset[str]] = {
    "listed": frozenset({"installed"}),
    "installed": frozenset(),
}


class InvalidMarketItemTransitionError(Exception):
    """Raised when a market-item status change is not an allowed edge in
    :data:`MARKET_ITEM_TRANSITIONS` (a re-install of an already-installed item,
    or any move out of the terminal ``installed`` state)."""


_MARKET_ITEM_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "kind",
    "version",
    "publisher",
    "description",
    "status",
    "provenance",
    "lock_version",
    "created_at",
    "updated_at",
)

_INSTALL_LOG_COLUMNS: tuple[str, ...] = (
    "id",
    "item_id",
    "actor",
    "version",
    "kind",
    "provenance",
    "installer",
    "detail",
    "metadata_json",
    "installed_at",
    "created_at",
)


# --------------------------------------------------------------------------
# market_item — create / get / list
# --------------------------------------------------------------------------


def create_market_item(
    db_path: Path,
    *,
    name: str,
    kind: str,
    version: str = "",
    publisher: str = "",
    description: str = "",
    provenance: str = "",
    status: str = "listed",
    item_id: str | None = None,
) -> dict:
    """Insert one ``market_item`` row and return it.

    ``kind`` must be one of :data:`MARKET_ITEM_KINDS` and ``status`` one of
    :data:`MARKET_ITEM_STATUSES` (defaulting to ``listed`` — a listing is
    registered before it is ever installed). ``name`` must be non-empty."""
    if not (name or "").strip():
        raise ValueError("market_item.name must be non-empty")
    if kind not in MARKET_ITEM_KINDS:
        raise ValueError(
            f"market_item.kind must be one of {sorted(MARKET_ITEM_KINDS)}, got {kind!r}"
        )
    if status not in MARKET_ITEM_STATUSES:
        raise ValueError(
            f"market_item.status must be one of {sorted(MARKET_ITEM_STATUSES)}, got {status!r}"
        )
    now = db.iso_now()
    record = {name_: None for name_ in _MARKET_ITEM_COLUMNS}
    record.update(
        {
            "id": item_id or db.new_id(),
            "name": name,
            "kind": kind,
            "version": version or "",
            "publisher": publisher or "",
            "description": description or "",
            "status": status,
            "provenance": provenance or "",
            "lock_version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(_MARKET_ITEM_COLUMNS)
    placeholders = ", ".join(f":{name_}" for name_ in _MARKET_ITEM_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO market_item ({columns}) VALUES ({placeholders})",
                record,
            )
    _mirror_market_item(record)
    return record


def _mirror_market_item(record: dict) -> None:
    """Best-effort dual-write of one market item into PostgreSQL (SRV-01B slice 8).

    After the authoritative commit and silent on failure. Parent of the install
    log's foreign key, so a lost write here costs every later log entry for the
    item — the compounding slice 5 measured.
    """
    try:
        from command_center.db.marketplace_store import PostgresMarketItemMirror

        PostgresMarketItemMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.warning("Could not mirror market_item into PostgreSQL", exc_info=True)


def _mirror_install_log(record: dict) -> None:
    """Best-effort dual-write of one install-log entry into PostgreSQL (slice 8).

    Runs after its item's mirror write, because the authority's own foreign key
    orders them that way and the hooks inherit that order.
    """
    try:
        from command_center.db.marketplace_store import PostgresInstallLogMirror

        PostgresInstallLogMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.warning("Could not mirror market_install_log into PostgreSQL", exc_info=True)



def get_market_item(db_path: Path, item_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM market_item WHERE id = ?", (item_id,)
        ).fetchone()
        return db._row_to_dict(row)


def list_market_items(
    db_path: Path,
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List catalogue items, newest first, optionally filtered by ``kind`` and/or
    ``status``. ``limit``/``offset`` page the result (stable order:
    ``created_at DESC, id DESC``)."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM market_item{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Atomic install: status transition + install-log append in one transaction
# --------------------------------------------------------------------------


def install_market_item(
    db_path: Path,
    item_id: str,
    *,
    expected_version: int,
    actor: str,
    installer: str,
    detail: str = "",
    metadata: dict[str, str] | None = None,
    log_id: str | None = None,
) -> tuple[dict, dict]:
    """Transition ``item_id`` ``listed → installed`` **and** append its
    install-log row inside one ``BEGIN IMMEDIATE`` transaction, returning
    ``(item_row, log_row)``.

    Atomic on purpose: the lifecycle change and its audit line are a single fact
    — a crash can never leave an installed item with no trail, or a trail line
    for an item that did not flip. Compare-and-set on ``lock_version`` guards a
    concurrent second writer; the status allowlist
    (:data:`MARKET_ITEM_TRANSITIONS`) refuses a re-install at the structural
    level (the service already turns a repeat install into a no-op above this).

    ``actor``/``installer`` must be non-empty — the log answers *who* installed
    and *by which installer*, so a blank either would defeat the record. The
    logged ``version``/``kind``/``provenance`` are copied from the item itself so
    the trail attributes exactly what was installed."""
    if not (actor or "").strip():
        raise ValueError("install actor must be non-empty (the log records who)")
    if not (installer or "").strip():
        raise ValueError("install installer must be non-empty")
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT * FROM market_item WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such market_item: {item_id!r}")
            if row["lock_version"] != expected_version:
                raise db.LostUpdateError(
                    f"market_item {item_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['lock_version']}"
                )
            if "installed" not in MARKET_ITEM_TRANSITIONS.get(row["status"], frozenset()):
                raise InvalidMarketItemTransitionError(
                    f"market_item {item_id!r} cannot transition "
                    f"{row['status']!r} -> 'installed'"
                )
            cur = conn.execute(
                "UPDATE market_item "
                "SET status = 'installed', updated_at = :now, "
                "lock_version = lock_version + 1 "
                "WHERE id = :item_id AND lock_version = :expected_version",
                {"now": now, "item_id": item_id, "expected_version": expected_version},
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"market_item {item_id!r} install affected {cur.rowcount} rows"
                )
            log_record = {
                "id": log_id or db.new_id(),
                "item_id": item_id,
                "actor": actor,
                "version": row["version"] or "",
                "kind": row["kind"],
                "provenance": row["provenance"] or "",
                "installer": installer,
                "detail": detail or "",
                "metadata_json": json.dumps(metadata or {}, sort_keys=True),
                "installed_at": now,
                "created_at": now,
            }
            columns = ", ".join(_INSTALL_LOG_COLUMNS)
            placeholders = ", ".join(f":{name_}" for name_ in _INSTALL_LOG_COLUMNS)
            conn.execute(
                f"INSERT INTO market_install_log ({columns}) VALUES ({placeholders})",
                log_record,
            )
            item_row = conn.execute(
                "SELECT * FROM market_item WHERE id = ?", (item_id,)
            ).fetchone()
            item_record = dict(item_row)
    # Item before log, both after the commit: the log's foreign key would
    # refuse a child whose parent is not mirrored yet.
    _mirror_market_item(item_record)
    _mirror_install_log(log_record)
    return item_record, dict(log_record)


def list_install_log(
    db_path: Path, item_id: str, *, limit: int = 100, offset: int = 0
) -> list[dict]:
    """Return the install-log rows for one item, newest first.

    Append-only history: ordered ``installed_at DESC, id DESC`` so the most
    recent install leads. ``metadata_json`` is decoded back to a dict under the
    ``metadata`` key so callers never re-parse it."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM market_install_log WHERE item_id = ? "
            "ORDER BY installed_at DESC, id DESC LIMIT ? OFFSET ?",
            (item_id, limit, offset),
        ).fetchall()
    decoded: list[dict] = []
    for row in rows:
        record = dict(row)
        try:
            record["metadata"] = json.loads(record.get("metadata_json") or "{}")
        except (ValueError, TypeError):
            record["metadata"] = {}
        decoded.append(record)
    return decoded
