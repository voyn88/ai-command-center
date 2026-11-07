"""Synchronous operator admission; no deployment or check execution."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import stat

from command_center.deployment import delivery_receipts as receipts


def _positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("revision must be positive")
    return number


def _hex(length):
    def parse(value):
        if not re.fullmatch(r"[a-f0-9]{%d}" % length, value):
            raise argparse.ArgumentTypeError("invalid digest")
        return value
    return parse


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("release", "acceptance", "corrective"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--task-id", required=True)
        cmd.add_argument("--revision", required=True, type=_positive)
        cmd.add_argument("--expected-sha", required=True, type=_hex(40))
        cmd.add_argument("--artifact-origin", required=True)
        if name != "corrective":
            cmd.add_argument("--repo", required=True)
        if name == "release":
            cmd.add_argument("--version-url", required=True)
            cmd.add_argument("--health-url", required=True)
            cmd.add_argument("--observation-origin", required=True)
            cmd.add_argument("--artifact-directory", required=True, type=Path)
            cmd.add_argument("--artifact-base-url", required=True)
        else:
            cmd.add_argument("--criterion-id", required=True)
            cmd.add_argument("--check-id", required=True)
            cmd.add_argument("--artifact-url", required=True)
            cmd.add_argument("--sha256", required=True, type=_hex(64))
        if name == "corrective":
            cmd.add_argument("--incident-key", required=True, type=_hex(64))
            cmd.add_argument("--corrective-task-id", required=True)
            cmd.add_argument("--corrective-revision", required=True, type=_positive)
    return result


def _authority(connection):
    # An authenticated operator login (including inherited operator membership),
    # never SET ROLE from an app/worker/admin connection.
    with connection.cursor() as cursor:
        cursor.execute("""SELECT session_user = current_user
            AND pg_has_role(session_user, 'aicc_operator', 'USAGE')
            AND NOT pg_has_role(session_user, 'aicc_app', 'MEMBER')
            AND NOT pg_has_role(session_user, 'aicc_worker', 'MEMBER')
            AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname = session_user)""")
        row = cursor.fetchone()
    if not row or row[0] is not True:
        raise ValueError("operator authority required")


def _pin_scope(connection, args):
    with connection.cursor() as cursor:
        # Check the operator expectation before any bounded network read.
        # SELECT FOR UPDATE is deliberately left to the existing SQL admission
        # function, rather than expanding operator table privileges.
        cursor.execute("SELECT delivery_team_release_scope(%s,%s)",
                       (args.task_id, args.revision))
        row = cursor.fetchone()
    scope = row[0] if row else None
    if (not isinstance(scope, dict) or scope.get("repo") != args.repo
            or scope.get("source_sha") != args.expected_sha):
        raise ValueError("operator scope mismatch")


def _read_basic_credentials(path):
    """Read only a bounded private regular file, refusing symlink substitution."""
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")
            or not callable(getattr(os, "geteuid", None))):
        raise ValueError("private authentication files require POSIX security capabilities")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_size > 4096):
            raise ValueError
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=receipts._unique_fields)
        if not isinstance(value, dict) or set(value) != {"username", "password"}:
            raise ValueError
        basic = (value["username"], value["password"])
        receipts._auth_headers(basic=basic)
        return basic
    except Exception:
        raise ValueError("invalid private authentication file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _authentication(args):
    mode = os.environ.get("AICC_TEAM_AUTH_MODE", "bearer")
    file = os.environ.get("AICC_TEAM_BASIC_CREDENTIALS_FILE")
    bearer = os.environ.get("AICC_TEAM_ARTIFACT_BEARER")
    observation = os.environ.get("AICC_TEAM_OBSERVATION_BEARER")
    if mode == "basic":
        if not file or bearer is not None or observation is not None:
            raise ValueError("conflicting authentication configuration")
        if (args.command == "release"
                and receipts._https_url(args.artifact_origin).netloc
                != receipts._https_url(args.observation_origin).netloc):
            raise ValueError("shared authentication requires the same HTTPS origin")
        return None, None, _read_basic_credentials(file)
    if mode != "bearer" or file is not None or not bearer:
        raise ValueError("invalid authentication configuration")
    if args.command == "release" and not observation:
        raise ValueError("missing observation credential")
    return bearer, observation, None


def admit(connection, args, *, artifact_bearer=None, observation_bearer=None, basic=None):
    """Caller owns the connection context/transaction; never emits credentials."""
    _authority(connection)
    read = receipts.configured_artifact_reader(
        args.artifact_origin, bearer=artifact_bearer, basic=basic)
    if args.command != "corrective":
        _pin_scope(connection, args)
    if args.command == "release":
        observe = receipts.configured_observer(
            args.observation_origin, bearer=observation_bearer, basic=basic)
        receipts.collect_release_receipt(
            connection, task_id=args.task_id, revision=args.revision,
            target=receipts.ReleaseTarget(args.repo, args.version_url, args.health_url),
            artifact_directory=args.artifact_directory,
            artifact_base_url=args.artifact_base_url,
            observe=observe, read_artifact=read)
    elif args.command == "acceptance":
        receipts.record_acceptance_artifact(
            connection, task_id=args.task_id, revision=args.revision,
            repo=args.repo, criterion_id=args.criterion_id, check_id=args.check_id,
            artifact_url=args.artifact_url, sha256=args.sha256, read=read)
    else:
        receipts.record_corrective_artifact(
            connection, parent_task_id=args.task_id, revision=args.revision,
            incident_key=args.incident_key, corrective_task_id=args.corrective_task_id,
            corrective_revision=args.corrective_revision, sha=args.expected_sha,
            criterion_id=args.criterion_id, check_id=args.check_id,
            artifact_url=args.artifact_url, sha256=args.sha256, read=read)
    # Re-read after network I/O: if authoritative release SHA moved meanwhile,
    # roll back instead of admitting against an operator's obsolete expectation.
    if args.command != "corrective":
        _pin_scope(connection, args)


def main(argv=None, *, connect=None):
    args = parser().parse_args(argv)
    try:
        # Fixed env names avoid secret-bearing CLI arguments and accidental
        # fallback to an app/worker's default PG connection.
        dsn = os.environ["AICC_TEAM_OPERATOR_DSN"]
        bearer, observation, basic = _authentication(args)
        if not dsn.strip():
            raise ValueError("missing operator configuration")
        if connect is None:
            import psycopg
            connect = psycopg.connect
        # psycopg connection context commits on success, rolls back on any
        # exception, and closes. No live connection occurs at import time.
        with connect(dsn, autocommit=False, connect_timeout=10,
                     options="-c statement_timeout=15000 -c lock_timeout=5000") as connection:
            admit(connection, args, artifact_bearer=bearer,
                  observation_bearer=observation, basic=basic)
    except Exception:
        # DSN, driver diagnostics and adapter exceptions may contain secrets.
        print("Operator admission failed; transaction not accepted.", file=sys.stderr)
        return 1
    print(json.dumps({"admitted": True, "command": args.command,
                      "task_id": args.task_id, "revision": args.revision,
                      "expected_sha": args.expected_sha}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
