"""Shared plumbing for the cross-host queue-claim proof.

`queue_claim_probe.py` is deliberately a standalone CLI with no import on this
package -- it is the payload that gets streamed, byte for byte, to whichever
host runs it (see its own docstring). Everything here is the *driver* side:
running that CLI locally, running it on a second real host over SSH without
staging anything on disk there first, and the handful of subprocess/DSN
helpers both the fixtures and the tests need.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

PROBE_PATH = Path(__file__).with_name("queue_claim_probe.py")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]


def ssh_reachable_with_live_systemd_user(target: str) -> bool:
    """Real precondition check, not a ping: can we SSH in *and* actually run a
    transient `systemd --user` unit there -- the same bar
    `tests/ops/test_worker_systemd_runtime_platform.py` sets for this host."""
    try:
        result = subprocess.run(
            [
                "ssh",
                *SSH_OPTS,
                target,
                "systemd-run",
                "--user",
                f"--unit=aicc-crosshost-probe-{uuid.uuid4().hex[:8]}",
                "--wait",
                "--",
                "/bin/true",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def override_dsn(dsn: str, **overrides: str) -> str:
    """`dsn` with the given libpq parameters replaced (URI or keyword form)."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(overrides)
    return make_conninfo(**params)


def _parse_last_json_line(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, (
        f"queue_claim_probe.py failed (rc={result.returncode}): "
        f"{result.stdout!r} {result.stderr!r}"
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"queue_claim_probe.py printed nothing: {result.stderr!r}"
    return json.loads(lines[-1])


def run_probe_local(dsn: str, host_label: str, *args: str, timeout: float = 30) -> dict:
    """Run the probe as a foreground subprocess on this (local) host."""
    result = subprocess.run(
        [sys.executable, str(PROBE_PATH), "--dsn", dsn, "--host-label", host_label, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return _parse_last_json_line(result)


def run_probe_remote(
    target: str, dsn: str, host_label: str, *args: str, timeout: float = 30
) -> dict:
    """Run the probe as a foreground subprocess on `target`, over SSH.

    The probe's own source is piped over the SSH session's stdin and executed
    with `python3 -`; nothing is written to the remote filesystem before or
    after. Only usable for calls that return promptly (`--hold-seconds 0`,
    the default) -- SSH does not return until the remote command exits.
    """
    argv = ["--dsn", dsn, "--host-label", host_label, *args]
    remote_cmd = "python3 - " + shlex.join(argv)
    result = subprocess.run(
        ["ssh", *SSH_OPTS, target, remote_cmd],
        input=PROBE_PATH.read_text(),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return _parse_last_json_line(result)


def stage_probe_remote(target: str) -> str:
    """Copy the probe's bytes to a unique path under /tmp on `target`.

    Needed only for the claim-and-hold case, where the probe must keep
    running as a trackable, killable unit after SSH itself has returned --
    `systemd-run`'s `ExecStart=` needs a real path, not stdin.
    """
    remote_path = f"/tmp/aicc-crosshost-probe-{uuid.uuid4().hex[:8]}.py"
    result = subprocess.run(
        ["ssh", *SSH_OPTS, target, f"cat > {shlex.quote(remote_path)}"],
        input=PROBE_PATH.read_text(),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"failed to stage probe on {target}: {result.stderr}"
    return remote_path


def unstage_probe_remote(target: str, remote_path: str) -> None:
    subprocess.run(
        ["ssh", *SSH_OPTS, target, "rm", "-f", remote_path],
        check=False,
        capture_output=True,
        timeout=10,
    )


def remote_unit_name(tag: str) -> str:
    return f"aicc-crosshost-verify-{tag}-{uuid.uuid4().hex[:8]}"


def start_remote_transient_unit(
    target: str, unit: str, properties: list[str], argv: list[str]
) -> subprocess.CompletedProcess:
    """Start a detached (`--no-block`) transient `--user` unit on `target`.

    Detached because the caller needs SSH to return immediately for a
    claim-and-hold process it intends to `SIGKILL` later -- `--wait` would
    block this call until that process exits on its own.
    """
    cmd = ["ssh", *SSH_OPTS, target, "systemd-run", "--user", f"--unit={unit}", "--no-block"]
    cmd += [f"--property={p}" for p in properties]
    cmd += ["--"] + argv
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=20)


def remote_unit_journal_json(target: str, unit: str) -> list[dict]:
    result = subprocess.run(
        [
            "ssh",
            *SSH_OPTS,
            target,
            "journalctl",
            "--user",
            "-u",
            f"{unit}.service",
            "-o",
            "json",
            "--no-pager",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def remote_unit_show(target: str, unit: str, *properties: str) -> dict[str, str]:
    result = subprocess.run(
        ["ssh", *SSH_OPTS, target, "systemctl", "--user", "show", f"{unit}.service"]
        + [f"-p{p}" for p in properties],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def kill_remote_unit(target: str, unit: str, signal_name: str = "SIGKILL") -> None:
    subprocess.run(
        ["ssh", *SSH_OPTS, target, "systemctl", "--user", "kill", f"--signal={signal_name}", f"{unit}.service"],
        check=False,
        capture_output=True,
        timeout=15,
    )


def stop_and_forget_remote_unit(target: str, unit: str) -> None:
    subprocess.run(
        ["ssh", *SSH_OPTS, target, "systemctl", "--user", "stop", f"{unit}.service"],
        check=False,
        capture_output=True,
        timeout=15,
    )
    subprocess.run(
        ["ssh", *SSH_OPTS, target, "systemctl", "--user", "reset-failed", f"{unit}.service"],
        check=False,
        capture_output=True,
        timeout=15,
    )
