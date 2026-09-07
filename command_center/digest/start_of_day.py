"""«Умный старт дня» — the owner's start-of-day priority list (VOYN-IOS-AUTO-HOME).

:func:`build_start_of_day_snapshot` merges the two Wave-1 owner-facing surfaces
that already exist — undone «Мой день» items (:mod:`.owner_autofill`) and the
already-built morning digest's ``attention`` entries (:mod:`.service`) — into
one ordered, bounded, *critical-first* list. This is the read contract a client
"smart start of day" screen renders on first open: an owner item with a ``due``
date outranks everything (soonest due first), then everything else newest
first (remaining undone owner items and attention-needing digest entries).

Why this satisfies "<2s on first open": the snapshot never makes a network call
and never triggers a digest build — it only reads rows the digest/owner-item
engines have *already persisted* (:meth:`DigestService.today`,
:func:`command_center.runtime.db.list_owner_items`), each capped at a small
limit. The latency budget is therefore bounded by one local read, by
construction, rather than by a timing assertion — timing assertions are flaky
on shared CI; the sibling engines' tests assert on structure instead, and this
module's tests do the same.

Delivery split: the on-device offline cache (SQLite/UserDefaults on iPhone,
populated from this snapshot and served from cache while the network is
unavailable) is client-side and out of scope for this repository — this module
is the server-side contract that cache would sync against, not the cache
itself.
"""

from __future__ import annotations

from pathlib import Path

from command_center.digest.service import CATEGORY_ATTENTION, DigestService, today_str
from command_center.models import SENSITIVE_PROJECT_IDS
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path

# Repo root is three levels up: <root>/command_center/digest/start_of_day.py
ROOT = Path(__file__).resolve().parents[2]

#: Bounds the priority list so one noisy source can never flood a first-open
#: screen — the acceptance criterion is a short, actionable list, not a dump.
MAX_CRITICAL = 20

#: How many undone owner items the read pulls before capping — generous enough
#: that a due-soon item near the tail of "newest first" is still seen.
_OWNER_ITEM_SCAN_LIMIT = 100


def _sensitive_projects() -> list[str]:
    return sorted(SENSITIVE_PROJECT_IDS)


def _db_path(root: Path) -> Path:
    path = resolve_db_path(root)
    if db.current_schema_version(path) < db.SCHEMA_VERSION:
        db.migrate(path)
    return path


def _critical_from_owner_item(row: dict) -> dict:
    return {
        "kind": "owner_item",
        "id": row["id"],
        "title": row.get("title") or "",
        "body": row.get("detail") or "",
        "due": row.get("due"),
        "refs": [f"owner-item:{row['id']}"],
        "created_at": row.get("created_at"),
    }


def _critical_from_digest_item(row: dict) -> dict:
    return {
        "kind": "digest_item",
        "id": row["id"],
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "due": None,
        "refs": list(row.get("refs") or []),
        "created_at": row.get("created_at"),
    }


def _has_due(entry: dict) -> bool:
    due = entry.get("due")
    return due is not None and str(due).strip() != ""


def _order_critical(entries: list[dict]) -> list[dict]:
    """Due-dated entries first (soonest due first), then the rest newest
    first. Two separate stable sorts, concatenated — simpler and more obviously
    correct than one composite sort key across two different orderings."""
    due_first = sorted((e for e in entries if _has_due(e)), key=lambda e: e["due"])
    rest = sorted(
        (e for e in entries if not _has_due(e)),
        key=lambda e: e.get("created_at") or "",
        reverse=True,
    )
    return due_first + rest


def build_start_of_day_snapshot(*, root: Path = ROOT, day: str | None = None) -> dict:
    """The owner's start-of-day snapshot: a bounded, priority-ordered list of
    critical items (due owner items, then other undone owner items, then
    attention-needing digest entries) plus the rest of today's already-built
    digest for context.

    Read-only and offline-computable: never builds or rebuilds the digest,
    never calls out to a network. Sensitive (BANK/LEGAL) rows are excluded, the
    same redaction the digest/owner-item read paths already apply."""
    path = _db_path(root)
    day = day or today_str()

    owner_rows = db.list_owner_items(
        path,
        done=False,
        exclude_projects=_sensitive_projects(),
        limit=_OWNER_ITEM_SCAN_LIMIT,
    )
    digest_rows = DigestService(root=root).today(day=day)
    attention_rows = [r for r in digest_rows if r.get("category") == CATEGORY_ATTENTION]
    context_rows = [r for r in digest_rows if r.get("category") != CATEGORY_ATTENTION]

    critical = [_critical_from_owner_item(r) for r in owner_rows]
    critical.extend(_critical_from_digest_item(r) for r in attention_rows)
    critical = _order_critical(critical)
    truncated = len(critical) > MAX_CRITICAL
    critical = critical[:MAX_CRITICAL]

    return {
        "day": day,
        "critical": critical,
        "digest": context_rows,
        "critical_truncated": truncated,
    }
