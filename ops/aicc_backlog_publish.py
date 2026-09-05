#!/usr/bin/env python3
"""Publish the canonical backlog into the control plane's structured store.

The control plane decides what to dispatch, review and merge from
`backlog_task`, and that table is filled by exactly one command:
`command_center.db backlog-import`. Nothing ever ran it on a schedule. The
result, measured live on 2026-08-31, was a control plane acting on a snapshot
five days stale: 32 tasks it had never seen, and a fully green pull request
that no tick could pick up because the task backing it still read `OPEN`.

Why a push from the machine that owns the file, rather than a timer on the
host: the canonical backlog is not in any repository and does not exist on
control-01 at all. A host-side timer would need a second copy to import from,
and a second copy is a second source of truth -- exactly the failure this is
meant to end. Here the file is read where it lives, its digest is taken there,
and the copy on the host exists only for the seconds between landing and
import.

Fail-closed at every step. The digest is verified *on the host* before the
import runs, so a truncated or garbled transfer is refused rather than
imported; the staging file is removed whether the import succeeded or not; and
a non-zero exit is returned for every failure, so a scheduler that only checks
status still learns that the store is no longer current.

This is one half of a deliberately temporary bidirectional bridge: the store
also exports back to the same markdown shape (`command_center/db/
backlog_export.py`, `backlog-export`, BO-S4) so readers of the file stay
current without the owner touching it. Running both directions at once is
safe only while the owner treats this file as input and never edits a
generated export directly -- see
docs/adr/0011-backlog-projection-bidirectional-bridge.md for the explicit
condition and date this script retires under.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_BACKLOG = Path(
    "/Users/dmitrijcernikov/Documents/Codex/2026-08-12/roadmap/outputs/VOYN_TASKS_BACKLOG.md"
)
DEFAULT_HOST = "root@100.114.209.119"
DEFAULT_REPO = "/home/voynadmin/aicc-preprod/repo"
DEFAULT_ENV = "/home/voynadmin/aicc-preprod/.env"
#: The unprivileged account the control plane's own units run as. The import
#: is run as that account, never as root: it must not be able to write
#: anything root can, and its database role is the one the store expects.
IMPORT_USER = "voynadmin"
SSH_TIMEOUT_SECONDS = 300


class PublishError(RuntimeError):
    """Anything that leaves the store not current. Always a non-zero exit."""


def _run(argv: list[str], *, stdin_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    with (stdin_path.open("rb") if stdin_path else _NoStdin()) as handle:
        return subprocess.run(
            argv,
            stdin=handle,
            capture_output=True,
            text=True,
            check=False,
            timeout=SSH_TIMEOUT_SECONDS,
        )


class _NoStdin:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        return None


def digest_of(path: Path) -> str:
    """SHA-256 of the file, read in chunks so a large backlog is never held
    in memory twice."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _staging_path(digest: str) -> str:
    # Named by digest and pid: two publishes cannot collide on one path, and
    # a leftover file names the content that produced it.
    return f"/tmp/aicc-backlog-{digest[:16]}-{os.getpid()}.md"


def publish(
    backlog: Path,
    *,
    host: str,
    repo: str,
    env_file: str,
    runner=_run,
) -> str:
    """Copy, verify, import, remove. Returns the import command's own report."""
    if not backlog.is_file():
        raise PublishError(f"canonical backlog is not a file: {backlog}")
    if backlog.stat().st_size == 0:
        raise PublishError(f"canonical backlog is empty: {backlog}")

    local_digest = digest_of(backlog)
    staged = _staging_path(local_digest)

    copied = runner(["scp", "-q", "-o", "ConnectTimeout=30", str(backlog), f"{host}:{staged}"])
    if copied.returncode != 0:
        raise PublishError(f"copy failed: {copied.stderr.strip() or copied.returncode}")

    try:
        # Verified on the host, against the digest taken here: a transfer that
        # lost or altered bytes must never reach the store.
        checked = runner(
            ["ssh", "-o", "ConnectTimeout=30", host, f"sha256sum {staged} | cut -d' ' -f1"]
        )
        if checked.returncode != 0:
            raise PublishError(f"digest check failed: {checked.stderr.strip()}")
        remote_digest = checked.stdout.strip()
        if remote_digest != local_digest:
            raise PublishError(
                f"digest mismatch: local {local_digest}, host {remote_digest}"
            )

        imported = runner(
            [
                "ssh",
                "-o",
                "ConnectTimeout=30",
                host,
                f"chown {IMPORT_USER}:{IMPORT_USER} {staged} && "
                f"su - {IMPORT_USER} -c "
                f"'cd {repo} && set -a && . {env_file} && set +a && "
                f"./.venv/bin/python -m command_center.db backlog-import {staged}'",
            ]
        )
        if imported.returncode != 0:
            raise PublishError(f"import failed: {imported.stderr.strip()[-400:]}")
        report = ""
        for line in imported.stdout.splitlines():
            if line.startswith("inserted "):
                report = line.strip()
        if not report:
            raise PublishError("import produced no report line")
        return report
    finally:
        # Removed whether the import succeeded or not: the copy exists only
        # for the duration of one publish, so it can never become a second
        # source anything reads later.
        runner(["ssh", "-o", "ConnectTimeout=30", host, f"rm -f {staged}"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backlog", type=Path, default=DEFAULT_BACKLOG)
    parser.add_argument("--host", default=os.environ.get("AICC_CONTROL_HOST", DEFAULT_HOST))
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--env-file", default=DEFAULT_ENV)
    args = parser.parse_args(argv)

    started = time.time()
    try:
        report = publish(
            args.backlog, host=args.host, repo=args.repo, env_file=args.env_file
        )
    except (PublishError, OSError, subprocess.SubprocessError) as exc:
        print(f"AICC_BACKLOG_PUBLISH_REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"AICC_BACKLOG_PUBLISHED {report} in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
