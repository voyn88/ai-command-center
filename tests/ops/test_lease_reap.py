"""VOYN-W0-AICC-REAPER-NOT-RUNNING: `ops/lease_reap.sh` must not depend on the
branch state of whatever directory it happens to be invoked from.

Found live on worker-01: the script used to `cd` into the shared preprod
checkout and let `voyn-lease acquire` derive its (synthetic) identity's
branch from whatever that checkout's HEAD pointed at. That checkout spent
2026-08-23T23:55Z onward in a detached-HEAD state -- unrelated to this
script -- where `git branch --show-current` prints nothing, and
`voyn-lease acquire` refuses an empty branch with `invalid branch`. Every
tick failed for 2.5+ days; nothing reaped any stuck lease in that window.

This test reproduces exactly that precondition (the script's cwd is a
detached-HEAD repo) against a stub `voyn-lease` that enforces the same
non-empty-branch rule the real tool does, and asserts a reap still succeeds
-- proving the fix (a dedicated identity repo the script controls and always
checks out onto a real branch) rather than merely asserting the new code
path exists.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "ops" / "lease_reap.sh"

_STUB_VOYN_LEASE = """#!/bin/bash
set -euo pipefail
case "$1" in
  list)
    echo '[{"repository_id":"demo-repo","expires_at":"2000-01-01T00:00:00+00:00"}]'
    ;;
  acquire)
    # The real tool's failure mode this test guards against: an empty
    # `git branch --show-current` in its (cwd-defaulted) --repo is refused.
    branch=$(git branch --show-current)
    if [ -z "$branch" ]; then
      echo "invalid branch" >&2
      exit 2
    fi
    echo '{"ok":true}'
    ;;
  release)
    echo '{"released":true}'
    ;;
  *)
    echo "unexpected verb: $1" >&2
    exit 1
    ;;
esac
"""


def _make_detached_head_repo(path: Path) -> None:
    path.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    run("init", "-q", "-b", "main")
    (path / "f").write_text("x")
    run("add", "f")
    run(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "-m",
        "init",
    )
    sha = run("rev-parse", "HEAD").stdout.strip()
    run("checkout", "-q", sha)  # detaches HEAD, exactly like the live incident


def test_reap_succeeds_when_invoked_from_a_detached_head_checkout(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "voyn-lease"
    stub.write_text(_STUB_VOYN_LEASE)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    identity_repo = tmp_path / "identity"
    log = tmp_path / "reap.log"
    detached_cwd = tmp_path / "detached-checkout"
    _make_detached_head_repo(detached_cwd)

    # Precondition this test exists to pin: the cwd the script is invoked
    # from really is branchless, the exact state that broke it in production.
    assert (
        subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=detached_cwd,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == ""
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=detached_cwd,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "IDENTITY_REPO": str(identity_repo),
            "LOG": str(log),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (result.stdout, result.stderr, log.read_text())
    log_text = log.read_text()
    assert "reaped demo-repo" in log_text
    assert "invalid branch" not in log_text

    # The identity repo is real, dedicated, and on an actual branch -- not
    # borrowed from (or broken by) the caller's cwd.
    assert (
        subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=identity_repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "lease-reaper"
    )


def test_reap_identity_repo_is_created_idempotently(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "voyn-lease"
    stub.write_text(_STUB_VOYN_LEASE)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    identity_repo = tmp_path / "identity"
    log = tmp_path / "reap.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "IDENTITY_REPO": str(identity_repo),
        "LOG": str(log),
    }

    first = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert first.returncode == 0, (first.stdout, first.stderr)
    head_after_first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=identity_repo, capture_output=True, text=True
    ).stdout.strip()

    second = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert second.returncode == 0, (second.stdout, second.stderr)
    head_after_second = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=identity_repo, capture_output=True, text=True
    ).stdout.strip()

    # Re-running the reap does not reinitialise or add commits to the
    # identity repo -- it is created once and then merely reused.
    assert head_after_first == head_after_second
