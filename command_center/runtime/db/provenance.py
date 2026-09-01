"""Provenance table-family: canonical run provenance (schema 13)
plus explicit provider route and immutable attempt evidence (schema 14)
(split out of the former single-file ``runtime/db.py``; pure move).

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes (``db.MIGRATIONS``, ``db.iso_now``,
``db._proposal_update``, ...) keep intercepting internal calls exactly as
they did against the single module.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import command_center.runtime.db as db  # facade (late-bound; see docstring)

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Canonical run provenance (schema 13)
# --------------------------------------------------------------------------


def backfill_run_provenance(db_path: Path, *, limit: int = 500) -> int:
    """Backfill at most ``limit`` legacy runs without inventing evidence.

    The historical ``run.repository_path`` named the process cwd, so it is a
    truthful worktree path but not necessarily the canonical repository. The
    latter therefore remains NULL. Completion facts are copied only when they
    were already persisted; accepted SHA is derived only from an explicit
    TARGET_VERIFIED terminal completion. Repeated calls insert only missing
    rows and are therefore idempotent.
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return 0
    now = db.iso_now()
    with db.connect(db_path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_provenance'"
        ).fetchone()
        if table is None:
            return 0
        with db.transaction(conn):
            cursor = conn.execute(
                """INSERT INTO run_provenance (
                       run_id, task_id, repository_path, worktree_path, branch,
                       base_branch, base_sha, head_sha, pull_request_number,
                       pull_request_url, pull_request_head_sha, accepted_sha,
                       accepted_at, created_at, updated_at
                   )
                   SELECT r.id, r.task_id, NULL, r.repository_path, c.branch,
                          c.base_branch, NULL, c.head_commit, c.pull_request_number,
                          c.pull_request_url,
                          CASE WHEN c.pull_request_number IS NOT NULL THEN c.head_commit END,
                          CASE
                              WHEN c.completion_state = 'COMPLETED'
                               AND c.last_reason_code = 'TARGET_VERIFIED'
                              THEN COALESCE(c.merge_commit, c.head_commit)
                          END,
                          CASE
                              WHEN c.completion_state = 'COMPLETED'
                               AND c.last_reason_code = 'TARGET_VERIFIED'
                              THEN c.updated_at
                          END,
                          r.created_at, ?
                   FROM run AS r
                   LEFT JOIN completion AS c ON c.run_id = r.id
                   WHERE NOT EXISTS (
                       SELECT 1 FROM run_provenance AS p WHERE p.run_id = r.id
                   )
                   ORDER BY r.created_at, r.rowid
                   LIMIT ?
                   RETURNING *""",
                (now, limit),
            )
            # `RETURNING *` gives the rows this statement created — exactly
            # them, and nothing else.
            #
            # The previous version read them back with `WHERE updated_at = ?`
            # and its comment claimed to be "the rows this INSERT just
            # created", bounded by `limit`. Independent acceptance measured
            # both claims false: `iso_now()` is second-precision, so a row
            # written by `update_run_provenance` a moment earlier, or by an
            # earlier backfill call in the same second, came back too. Every
            # row read was still a current authority row, so the consequence
            # was over-mirroring rather than corruption — but a comment
            # describing a set the query does not return is the defect class
            # this migration keeps rejecting, and on a busy system the set it
            # returns is bounded by how many rows share a second rather than
            # by `limit`.
            backfilled = [dict(row) for row in cursor.fetchall()]
            inserted = len(backfilled)
    for record in backfilled:
        _mirror("PostgresRunProvenanceMirror", record, "run_provenance")
    return inserted


def get_run_provenance(db_path: Path, run_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_provenance WHERE run_id = ?", (run_id,)
        ).fetchone()
        return db._row_to_dict(row)


def get_run_provenance_for_runs(db_path: Path, run_ids: list[str]) -> dict[str, dict]:
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM run_provenance WHERE run_id IN ({placeholders})",
            tuple(run_ids),
        ).fetchall()
    return {row["run_id"]: dict(row) for row in rows}


def update_run_provenance(db_path: Path, run_id: str, *, fields: dict) -> dict:
    allowed = {
        "repository_path",
        "worktree_path",
        "branch",
        "base_branch",
        "base_sha",
        "head_sha",
        "pull_request_number",
        "pull_request_url",
        "pull_request_head_sha",
        "ci_conclusions_json",
        "ci_observed_at",
        "accepted_sha",
        "accepted_at",
        "deployed_sha",
        "deployment_environment",
        "deployed_at",
        "deployment_verified_at",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise db.UnknownRunFieldError(f"Not an updatable provenance field: {sorted(unknown)}")
    if not fields:
        row = db.get_run_provenance(db_path, run_id)
        if row is None:
            raise KeyError(f"No provenance for run: {run_id!r}")
        return row
    values = dict(fields)
    values["updated_at"] = db.iso_now()
    set_clause = ", ".join(f"{key} = :{key}" for key in values)
    values["run_id"] = run_id
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            cursor = conn.execute(
                f"UPDATE run_provenance SET {set_clause} WHERE run_id = :run_id", values
            )
            if cursor.rowcount != 1:
                raise KeyError(f"No provenance for run: {run_id!r}")
            row = conn.execute(
                "SELECT * FROM run_provenance WHERE run_id = ?", (run_id,)
            ).fetchone()
            record = dict(row)
    _mirror("PostgresRunProvenanceMirror", record, "run_provenance")
    return record


def _mirror(mirror_name: str, record: dict, table: str) -> None:
    """Best-effort dual-write of one provenance row into PostgreSQL (slice 13).

    One helper for four tables because the rule is identical for all of them
    and repeating it four times would be four places to fix the next time.
    After the authoritative commit, silent on failure, and lazily imported so
    the desktop and CLI entry points keep working without a driver.
    """
    try:
        from command_center.db import provenance_store

        getattr(provenance_store, mirror_name)().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.warning("Could not mirror %s into PostgreSQL", table, exc_info=True)


def set_run_provenance_once(
    db_path: Path,
    run_id: str,
    *,
    field: str,
    value: str,
    fields: dict,
) -> tuple[dict, bool]:
    """Atomically set an immutable provenance fact once.

    ``matched`` is false only when another immutable value already exists.
    A replay of the same value is idempotent and does not rewrite timestamps.
    """
    if field not in {"accepted_sha", "deployed_sha"}:
        raise db.UnknownRunFieldError(f"Not an immutable provenance field: {field!r}")
    if fields.get(field) != value:
        raise ValueError(f"fields must bind {field!r} to the immutable value")
    allowed = {
        "accepted_sha",
        "accepted_at",
        "deployed_sha",
        "deployment_environment",
        "deployed_at",
        "deployment_verified_at",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise db.UnknownRunFieldError(f"Not an immutable provenance field: {sorted(unknown)}")
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT * FROM run_provenance WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No provenance for run: {run_id!r}")
            current = dict(row)
            existing = current.get(field)
            if existing is not None:
                return current, existing == value
            values = dict(fields)
            values["updated_at"] = db.iso_now()
            values["run_id"] = run_id
            set_clause = ", ".join(f"{key} = :{key}" for key in values if key != "run_id")
            conn.execute(
                f"UPDATE run_provenance SET {set_clause} WHERE run_id = :run_id", values
            )
            updated = conn.execute(
                "SELECT * FROM run_provenance WHERE run_id = ?", (run_id,)
            ).fetchone()
            record = dict(updated)
    _mirror("PostgresRunProvenanceMirror", record, "run_provenance")
    return record, True


def create_provenance_evidence(
    db_path: Path,
    *,
    run_id: str,
    integrity_id: str,
    adapter: str,
    status: str,
    candidate_sha: str | None,
    reported_sha: str | None,
    native_payload_json: str,
    normalized_json: str,
    observed_at: str,
) -> dict:
    """Insert one native evidence event, or return its exact prior record.

    Replaying the same integrity id with different content is rejected rather
    than silently rewriting audit history.
    """
    record = {
        "integrity_id": integrity_id,
        "run_id": run_id,
        "adapter": adapter,
        "status": status,
        "candidate_sha": candidate_sha,
        "reported_sha": reported_sha,
        "native_payload_json": native_payload_json,
        "normalized_json": normalized_json,
        "observed_at": observed_at,
    }
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            existing = conn.execute(
                "SELECT * FROM provenance_evidence WHERE integrity_id = ?",
                (integrity_id,),
            ).fetchone()
            if existing is not None:
                current = dict(existing)
                comparable = {key: current[key] for key in record}
                if comparable != record:
                    raise ValueError(
                        f"Evidence integrity id {integrity_id!r} already has different content"
                    )
                return current
            conn.execute(
                """INSERT INTO provenance_evidence (
                       integrity_id, run_id, adapter, status, candidate_sha,
                       reported_sha, native_payload_json, normalized_json, observed_at
                   ) VALUES (
                       :integrity_id, :run_id, :adapter, :status, :candidate_sha,
                       :reported_sha, :native_payload_json, :normalized_json, :observed_at
                   )""",
                record,
            )
    _mirror("PostgresProvenanceEvidenceMirror", record, "provenance_evidence")
    return record


def list_provenance_evidence_stored(db_path: Path) -> list[dict]:
    """Every evidence row in the shape SQLite **stores**, for reconciliation.

    :func:`get_provenance_evidence_for_runs` selects an explicit column list
    that omits both `jsonb` payloads — the read surface does not need them, and
    they are large. Reconciliation does need them: fed the projected rows it
    would report every evidence row divergent on two columns at once.

    The fitness gate caught this before any test did, which is what it is for:
    the same rule that found `run_event`'s projected `id` one slice earlier.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM provenance_evidence ORDER BY integrity_id"
        ).fetchall()
        return [dict(row) for row in rows]


def get_provenance_evidence_for_runs(
    db_path: Path, run_ids: list[str]
) -> dict[str, list[dict]]:
    """Return safe evidence envelopes for the canonical read model.

    Native and normalized JSON payloads deliberately stay inside persistence:
    the Dashboard needs adapter/status/SHA identity, never prompts or provider
    payload fields.
    """
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT integrity_id, run_id, adapter, status, candidate_sha,
                       reported_sha, observed_at
                FROM provenance_evidence
                WHERE run_id IN ({placeholders})
                ORDER BY run_id, observed_at, integrity_id""",
            tuple(run_ids),
        ).fetchall()
    result: dict[str, list[dict]] = {run_id: [] for run_id in run_ids}
    for row in rows:
        result[row["run_id"]].append(dict(row))
    return result


# --------------------------------------------------------------------------
# Explicit provider route and immutable attempt evidence (schema 14)
# --------------------------------------------------------------------------


def get_provider_route(db_path: Path, run_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_provider_route WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["providers"] = json.loads(result.pop("providers_json"))
    return result


def list_provider_routes_stored(db_path: Path) -> list[dict]:
    """Every route row in the shape SQLite **stores**, for reconciliation.

    Both public readers decode inline — `result["providers"] =
    json.loads(result.pop("providers_json"))` — so the column the mirror holds
    is gone from what they return, and reconciliation fed those rows reports
    every route divergent on `providers_json` while agreeing about a column
    named `providers` that PostgreSQL does not have.

    Found by the fitness gate after it was taught to recognise inline
    decoding: it knew `_decode_*` helpers and projected `SELECT` lists, and
    this is the third variant of the same act written a third way.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_provider_route ORDER BY run_id"
        ).fetchall()
        return [dict(row) for row in rows]


def get_provider_routes_for_runs(db_path: Path, run_ids: list[str]) -> dict[str, dict]:
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM run_provider_route WHERE run_id IN ({placeholders})",
            tuple(run_ids),
        ).fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        item["providers"] = json.loads(item.pop("providers_json"))
        result[item["run_id"]] = item
    return result


def start_provider_attempt(
    db_path: Path,
    *,
    run_id: str,
    attempt_number: int,
    provider_id: str,
    started_at: str,
) -> dict:
    record = {
        "run_id": run_id,
        "attempt_number": attempt_number,
        "provider_id": provider_id,
        "outcome": "started",
        "classification": None,
        "disposition": None,
        "error_code": None,
        "parent_attempt_number": attempt_number - 1 if attempt_number > 1 else None,
        "started_at": started_at,
        "completed_at": None,
    }
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            existing = conn.execute(
                """SELECT * FROM provider_attempt
                   WHERE run_id = ? AND attempt_number = ?""",
                (run_id, attempt_number),
            ).fetchone()
            if existing is not None:
                current = dict(existing)
                if current != record:
                    raise ValueError(
                        f"Provider attempt {run_id!r}/{attempt_number} already differs"
                    )
                return current
            conn.execute(
                """INSERT INTO provider_attempt (
                       run_id, attempt_number, provider_id, outcome,
                       classification, disposition, error_code,
                       parent_attempt_number, started_at, completed_at
                   ) VALUES (
                       :run_id, :attempt_number, :provider_id, :outcome,
                       :classification, :disposition, :error_code,
                       :parent_attempt_number, :started_at, :completed_at
                   )""",
                record,
            )
    _mirror("PostgresProviderAttemptMirror", record, "provider_attempt")
    return record


def finish_provider_attempt(
    db_path: Path,
    *,
    run_id: str,
    attempt_number: int,
    outcome: str,
    classification: str,
    disposition: str,
    error_code: str | None,
    completed_at: str,
) -> dict:
    if outcome not in {"succeeded", "failed", "cancelled"}:
        raise ValueError(f"Invalid provider attempt outcome: {outcome!r}")
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                """SELECT * FROM provider_attempt
                   WHERE run_id = ? AND attempt_number = ?""",
                (run_id, attempt_number),
            ).fetchone()
            if row is None:
                raise KeyError(f"No provider attempt {run_id!r}/{attempt_number}")
            current = dict(row)
            expected = {
                **current,
                "outcome": outcome,
                "classification": classification,
                "disposition": disposition,
                "error_code": error_code,
                "completed_at": completed_at,
            }
            if current["outcome"] != "started":
                if current != expected:
                    raise ValueError(
                        f"Provider attempt {run_id!r}/{attempt_number} is immutable"
                    )
                return current
            conn.execute(
                """UPDATE provider_attempt
                   SET outcome = ?, classification = ?, disposition = ?,
                       error_code = ?, completed_at = ?
                   WHERE run_id = ? AND attempt_number = ? AND outcome = 'started'""",
                (
                    outcome,
                    classification,
                    disposition,
                    error_code,
                    completed_at,
                    run_id,
                    attempt_number,
                ),
            )
            updated = conn.execute(
                """SELECT * FROM provider_attempt
                   WHERE run_id = ? AND attempt_number = ?""",
                (run_id, attempt_number),
            ).fetchone()
            record = dict(updated)
    _mirror("PostgresProviderAttemptMirror", record, "provider_attempt")
    return record


def list_provider_attempts(db_path: Path, run_id: str) -> list[dict]:
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM provider_attempt
               WHERE run_id = ? ORDER BY attempt_number""",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_provider_attempts_for_runs(
    db_path: Path, run_ids: list[str]
) -> dict[str, list[dict]]:
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT * FROM provider_attempt
                WHERE run_id IN ({placeholders})
                ORDER BY run_id, attempt_number""",
            tuple(run_ids),
        ).fetchall()
    result: dict[str, list[dict]] = {run_id: [] for run_id in run_ids}
    for row in rows:
        result[row["run_id"]].append(dict(row))
    return result
