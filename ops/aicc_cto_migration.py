"""Approved AICC lineage preflight over the existing AIOS migration runner.

No default target, live-manifest generator, ledger repair or replacement engine.
The CLI is deliberately separate from the ordinary database upgrade command.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import tempfile

from command_center.db import adapter, migrations


class Refusal(ValueError):
    """A fixed, non-secret refusal code suitable for CLI output."""


IDENTITY_SQL = """
SELECT current_database(), inet_server_addr()::text, inet_server_port(),
       (pg_control_system()).system_identifier::text,
       (SELECT oid::bigint FROM pg_database WHERE datname=current_database()),
       current_setting('server_version_num')::integer / 10000,
       session_user
"""
LEDGER_SQL = "SELECT version, slug, checksum FROM public.schema_migration ORDER BY version"
# Only structure, not row contents, ownership, OIDs, ACLs or statistics. All
# comparison order is imposed in Python, independent of database collation.
CATALOG_SQL = """
WITH objects AS (
 SELECT 'relation' AS kind, c.relname AS name,
  jsonb_build_object('kind',c.relkind,'persistence',c.relpersistence,
   'row_security',c.relrowsecurity,'force_row_security',c.relforcerowsecurity,
   'options',c.reloptions,'partition_bound',pg_get_expr(c.relpartbound,c.oid),
   'partition_key',CASE WHEN c.relkind='p' THEN pg_get_partkeydef(c.oid) END,
   'view',CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid,false) END) AS definition
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
 UNION ALL
 SELECT 'column', c.relname||'.'||a.attname,
  jsonb_build_object('position',a.attnum,'type',format_type(a.atttypid,a.atttypmod),
   'not_null',a.attnotnull,'identity',a.attidentity,'generated',a.attgenerated,
   'default',pg_get_expr(d.adbin,d.adrelid))
 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace
 LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
 WHERE n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped
 UNION ALL
 SELECT 'constraint', (CASE WHEN c.conrelid<>0 THEN c.conrelid::regclass::text ELSE c.contypid::regtype::text END)||'.'||c.conname,
  jsonb_build_object('definition',pg_get_constraintdef(c.oid,false),'validated',c.convalidated)
 FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public'
 UNION ALL
 SELECT 'index', c.relname, jsonb_build_object('definition',pg_get_indexdef(c.oid),
   'valid',i.indisvalid,'ready',i.indisready)
 FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid
 JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
 UNION ALL
 SELECT 'trigger',c.relname||'.'||t.tgname,
  jsonb_build_object('definition',pg_get_triggerdef(t.oid,false),'enabled',t.tgenabled)
 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal
 UNION ALL
 SELECT 'routine',p.proname||'('||pg_get_function_identity_arguments(p.oid)||')',
  jsonb_build_object('kind',p.prokind,'definition',CASE WHEN p.prokind IN ('f','p') THEN pg_get_functiondef(p.oid) ELSE p.prosrc END)
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public'
 UNION ALL
 SELECT 'rule',c.relname||'.'||r.rulename,jsonb_build_object('definition',pg_get_ruledef(r.oid,false),'enabled',r.ev_enabled)
 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
 JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
 UNION ALL
 SELECT 'type',t.typname,jsonb_build_object('kind',t.typtype,'category',t.typcategory,
  'base',format_type(t.typbasetype,t.typtypmod),'not_null',t.typnotnull,'default',t.typdefault,
  'enum',(SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid=t.oid))
 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public'
 UNION ALL
 SELECT 'sequence',c.relname,jsonb_build_object('type',format_type(s.seqtypid,NULL),
  'start',s.seqstart,'increment',s.seqincrement,'max',s.seqmax,'min',s.seqmin,
  'cache',s.seqcache,'cycle',s.seqcycle)
 FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
 UNION ALL
 SELECT 'policy',c.relname||'.'||p.polname,jsonb_build_object('command',p.polcmd,
  'permissive',p.polpermissive,'using',pg_get_expr(p.polqual,p.polrelid),
  'check',pg_get_expr(p.polwithcheck,p.polrelid),
  'roles',(SELECT jsonb_agg(CASE WHEN r=0 THEN 'public' ELSE pg_get_userbyid(r) END ORDER BY CASE WHEN r=0 THEN 'public' ELSE pg_get_userbyid(r) END)
   FROM unnest(p.polroles) r))
 FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
)
SELECT kind,name,definition FROM objects
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("duplicate_manifest_key")
        result[key] = value
    return result


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _keys(value, expected):
    return isinstance(value, dict) and set(value) == set(expected)


def load_manifest(path: Path, digest: str) -> dict:
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024 or not _hash(digest) or _sha(raw) != digest:
        raise Refusal("manifest_hash_mismatch")
    manifest = json.loads(raw, object_pairs_hook=_object)
    if not _keys(manifest, ("schema", "target", "identity", "migrations", "catalog_sha256", "execution_host", "connection_host", "connection_port", "identity_evidence_sha256", "connection_evidence_sha256", "migration_session_user")):
        raise Refusal("invalid_manifest")
    if manifest["schema"] != "aicc-cto-migration/v1" or type(manifest["target"]) is not int or manifest["target"] != 36:
        raise Refusal("invalid_target")
    identity = manifest["identity"]
    if not _keys(identity, ("dbname", "server_address", "server_port", "system_identifier", "database_oid", "server_major")):
        raise Refusal("invalid_identity")
    if identity["dbname"] != "aicc_preprod" or (identity["server_address"] is not None and (not isinstance(identity["server_address"], str) or not identity["server_address"].strip())):
        raise Refusal("invalid_identity")
    if not isinstance(identity["system_identifier"], str) or not re.fullmatch(r"[1-9][0-9]+", identity["system_identifier"]):
        raise Refusal("invalid_identity")
    if any(type(identity[k]) is not int or identity[k] <= 0 for k in ("server_port", "database_oid", "server_major")):
        raise Refusal("invalid_identity")
    if not all(isinstance(manifest[k], str) and manifest[k].strip() for k in ("execution_host", "connection_host")) or not _hash(manifest["identity_evidence_sha256"]):
        raise Refusal("invalid_connection_pin")
    if type(manifest["connection_port"]) is not int or manifest["connection_port"] != identity["server_port"]:
        raise Refusal("invalid_connection_pin")
    if identity["server_address"] is None and not manifest["connection_host"].startswith("/"):
        raise Refusal("invalid_socket_pin")
    if not _hash(manifest["connection_evidence_sha256"]) or not isinstance(manifest["migration_session_user"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", manifest["migration_session_user"]):
        raise Refusal("invalid_migration_login_pin")
    rows = manifest["migrations"]
    if not isinstance(rows, list) or len(rows) != 36:
        raise Refusal("invalid_lineage")
    for version, row in enumerate(rows, 1):
        if not _keys(row, ("version", "slug", "checksum", "down_checksum")) or type(row["version"]) is not int or row["version"] != version:
            raise Refusal("invalid_lineage")
        if not isinstance(row["slug"], str) or not re.fullmatch(r"[a-z0-9_]+", row["slug"]) or not _hash(row["checksum"]) or not _hash(row["down_checksum"]):
            raise Refusal("invalid_lineage")
    catalogs = manifest["catalog_sha256"]
    if not _keys(catalogs, ("34", "35")) or not all(_hash(v) for v in catalogs.values()):
        raise Refusal("invalid_catalog_manifest")
    return manifest


def validate_identity_evidence(path: Path, manifest: dict) -> None:
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024 or _sha(raw) != manifest["identity_evidence_sha256"]:
        raise Refusal("identity_evidence_hash_mismatch")
    evidence = json.loads(raw, object_pairs_hook=_object)["manifest"]
    cluster, database = evidence["cluster_identity"], evidence["database_identity"]
    identity = manifest["identity"]
    if (cluster["system_identifier"] != identity["system_identifier"]
        or int(database["oid"]) != identity["database_oid"]
        or database["name"] != identity["dbname"]
        or int(cluster["server_version_num"]) // 10000 != identity["server_major"]
        or int(cluster["port"]) != identity["server_port"]
        or evidence["host_observation"]["hostname"] != manifest["execution_host"]):
        raise Refusal("identity_evidence_mismatch")
    expected = [(m["version"], m["slug"], m["checksum"]) for m in manifest["migrations"][:34]]
    actual = [(m["version"], m["slug"], m["checksum"]) for m in evidence["ledger"]]
    if actual != expected:
        raise Refusal("identity_evidence_lineage_mismatch")


def validate_connection_evidence(path: Path, manifest: dict) -> None:
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024 or _sha(raw) != manifest["connection_evidence_sha256"]:
        raise Refusal("connection_evidence_hash_mismatch")
    evidence = json.loads(raw, object_pairs_hook=_object)
    approved = evidence["approved_manifest"]
    selection, observed = evidence["selection"], evidence["snapshot"]
    identity = manifest["identity"]
    if (approved["sha256"] != manifest["identity_evidence_sha256"]
        or approved["database"] != identity["dbname"]
        or int(approved["database_oid"]) != identity["database_oid"]
        or approved["system_identifier"] != identity["system_identifier"]
        or observed["hostname"] != manifest["execution_host"]
        or int(observed["port_show"]) != manifest["connection_port"]
        or selection["directory"] != manifest["connection_host"]
        or selection["exists"] is not True or selection["is_socket"] is not True):
        raise Refusal("connection_evidence_mismatch")


def verify_snapshot(snapshot: Path, manifest: dict) -> None:
    found = migrations.discover(snapshot)
    if tuple(m.version for m in found) != tuple(range(1, 37)):
        raise Refusal("source_lineage_mismatch")
    for migration, expected in zip(found, manifest["migrations"], strict=True):
        if migration.slug != expected["slug"] or migration.checksum != expected["checksum"] or _sha(migration.down_sql.encode()) != expected["down_checksum"]:
            raise Refusal("source_checksum_mismatch")


@contextmanager
def approved_source(sql_dir: Path, manifest: dict):
    """Snapshot verified bytes so the runner never rereads mutable source SQL."""
    found = migrations.discover(sql_dir)
    if tuple(m.version for m in found) != tuple(range(1, 37)):
        raise Refusal("source_lineage_mismatch")
    with tempfile.TemporaryDirectory(prefix="aicc-approved-sql-") as temporary:
        snapshot = Path(temporary)
        for migration, expected in zip(found, manifest["migrations"], strict=True):
            if migration.slug != expected["slug"]:
                raise Refusal("source_slug_mismatch")
            for direction, source, key in (("up", migration.up_path, "checksum"), ("down", migration.down_path, "down_checksum")):
                raw = source.read_bytes()
                if b"\r" in raw or _sha(raw) != expected[key]:
                    raise Refusal("source_checksum_mismatch")
                raw.decode("utf-8")
                target = snapshot / f"{migration.name}.{direction}.sql"
                target.write_bytes(raw)
                target.chmod(0o400)
        yield snapshot


def catalog_hash(rows) -> str:
    normalized = []
    for kind, name, definition in rows:
        if isinstance(definition, str):
            definition = json.loads(definition)
        normalized.append([kind, name, definition])
    normalized.sort(key=lambda row: (row[0], row[1]))
    return _sha(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def validate_live(conn, manifest: dict) -> int:
    """Read only; caller must already hold the shared migration session lock."""
    if socket.gethostname() != manifest["execution_host"] or conn.info.host != manifest["connection_host"] or conn.info.port != manifest["connection_port"]:
        raise Refusal("connection_target_mismatch")
    conn.execute("SET search_path TO public")
    observed = conn.execute(IDENTITY_SQL).fetchone()
    expected = manifest["identity"]
    identity = tuple(expected[k] for k in ("dbname", "server_address", "server_port", "system_identifier", "database_oid", "server_major"))
    if expected["server_address"] is None and observed is not None:
        observed = (*observed[:2], int(conn.info.port), *observed[3:])
    if observed is None or tuple(observed[:6]) != identity or observed[6] != manifest["migration_session_user"]:
        raise Refusal("target_identity_mismatch")
    rows = conn.execute(LEDGER_SQL).fetchall()
    if len(rows) not in (34, 35):
        raise Refusal("unsupported_live_lineage")
    approved = [(m["version"], m["slug"], m["checksum"]) for m in manifest["migrations"][:len(rows)]]
    if [tuple(row) for row in rows] != approved:
        raise Refusal("live_checksum_mismatch")
    if catalog_hash(conn.execute(CATALOG_SQL).fetchall()) != manifest["catalog_sha256"][str(len(rows))]:
        raise Refusal("schema_artifact_mismatch")
    return len(rows)


def execute(conn, manifest: dict, snapshot: Path, *, apply: bool = False, lock_wait_seconds: int = 10) -> dict:
    if not conn.autocommit:
        raise Refusal("autocommit_required")
    if type(lock_wait_seconds) is not int or not 1 <= lock_wait_seconds <= 60:
        raise Refusal("invalid_lock_wait")
    with adapter.advisory_lock(conn, adapter.lock_key(migrations.LOCK_NAMESPACE), wait_timeout=lock_wait_seconds):
        baseline = validate_live(conn, manifest)
        applied = ()
        if apply:
            verify_snapshot(snapshot, manifest)
            # The AIOS runner takes this SAME session lock reentrantly. Its
            # inner unlock leaves our outer lock held across ledger verification.
            applied = migrations.runner(snapshot).upgrade(conn, target=36)
            final = conn.execute(LEDGER_SQL).fetchall()
            expected = [(m["version"], m["slug"], m["checksum"]) for m in manifest["migrations"]]
            if [tuple(row) for row in final] != expected:
                raise Refusal("post_upgrade_lineage_mismatch")
        return {"mode": "apply" if apply else "plan", "baseline": baseline,
                "target": 36, "pending": list(range(baseline + 1, 37)), "applied": list(applied)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--sql-dir", required=True, type=Path)
    parser.add_argument("--identity-evidence", required=True, type=Path)
    parser.add_argument("--connection-evidence", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    # This standalone CLI must not leak DSN-bearing pool/driver diagnostics,
    # including messages from pool background threads. Return fixed codes only.
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    pool = None
    try:
        manifest = load_manifest(args.manifest, args.manifest_sha256)
        validate_identity_evidence(args.identity_evidence, manifest)
        validate_connection_evidence(args.connection_evidence, manifest)
        dsn = os.environ.get("AICC_CTO_MIGRATION_DSN")
        if not dsn:
            raise Refusal("missing_dedicated_dsn")
        with approved_source(args.sql_dir, manifest) as snapshot:
            pool = adapter.open_pool(dsn, min_size=1, max_size=1, timeout=10,
                                     checkout_timeout=10, autocommit=True,
                                     name="aicc-cto-migration")
            with pool.connection() as conn:
                result = execute(conn, manifest, snapshot, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Refusal as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "reason": "migration_preflight_or_apply_failed"}))
        return 1
    finally:
        if pool is not None:
            try:
                pool.close()
            except Exception:
                pass
        logging.disable(previous_logging_disable)


if __name__ == "__main__":
    raise SystemExit(main())
