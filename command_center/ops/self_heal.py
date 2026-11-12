"""Self-heal: restart a small allow-list of services on clear signs of a hang.

The incidents this exists for are real and repeated: ``tailscaled`` holding the
review tunnel's port so the control loop could not publish verdicts for days;
a wedged ``sshd``/tailnet that nobody with root could restart.  The cure each
time was a single ``systemctl restart``.

Rules that keep it from becoming a second incident:

- ONLY the listed actions exist (``tailscaled``, ``ssh``, and allow-listed
  ``voyn-aicc-*.service`` units found in the ``failed`` state).  A unit name is
  matched against a strict pattern and a deny-list (worker lane instances and
  the self-deploy unit are never restarted from here);
- a signal must be seen on TWO CONSECUTIVE runs before anything happens (one
  bad probe is noise);
- one action per SERVICE at most every 30 minutes, at most three actions per
  hour and six per day -- after that the guard halts itself for 24 hours and
  the status beacon says so; a restart that does not cure the fault is not
  repeated forever;
- dry run is the default: it journals the action it WOULD take.
  ``OPS_SELF_HEAL_DRY_RUN=0`` turns it live;
- a tailscaled that the owner stopped (or that needs a login) is left alone:
  a restart cannot help and would only get in the way;
- the tunnel signal fires only when the review port is HELD BY ``tailscaled``
  (the known fault).  A port nobody listens on means the owner's machine is
  away, which no restart here can fix.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from command_center.ops.autonomy_common import (
    DEFAULT_LOG_DIR,
    DEFAULT_STATE_DIR,
    Journal,
    Runner,
    StateFile,
    as_dry_run,
    as_int,
    exclusive_lock,
    load_settings,
    run_command,
)
from command_center.ops.status_beacon import _tcp_banner, holder_word, tunnel_held_by

COOLDOWN_SECONDS = 30 * 60
MAX_ACTIONS_PER_HOUR = 3
#: one hard stop per day: after this many restarts the guard halts itself for
#: 24 hours and says so (finding C5); a restart that never cures is not retried
#: forever.
MAX_ACTIONS_PER_DAY = 6
HALT_SECONDS = 24 * 3600
#: tailscaled states that mean the OWNER stopped it or it needs a login: a
#: restart cannot help and would only get in the way.
OWNER_STOPPED_STATES = frozenset({"Stopped", "NeedsLogin", "NeedsMachineAuth"})
CONFIRM_RUNS = 2
ALLOWED_UNIT = re.compile(r"^(tailscaled|ssh|voyn-aicc-[a-z0-9._-]+)\.service$")
DENIED_UNIT = re.compile(r"(worker@|self-deploy|pgtunnel|credential)")
DEFAULT_HEAL_UNITS = (
    "voyn-aicc-review.service",
    "voyn-aicc-merge.service",
    "voyn-aicc-pr-window.service",
    "voyn-aicc-remediate.service",
    "voyn-aicc-source-clone-refresh.service",
)


def unit_allowed(unit: str) -> bool:
    return bool(ALLOWED_UNIT.match(unit)) and not DENIED_UNIT.search(unit)


def tailscale_backend_state(runner: Runner) -> str | None:
    code, out, _ = runner(["tailscale", "status", "--json"], 15)
    if code != 0:
        return None
    try:
        state = json.loads(out).get("BackendState")
    except ValueError, AttributeError:
        return None
    return state if isinstance(state, str) else None


def tailscale_unresponsive(runner: Runner) -> bool:
    code, out, _ = runner(["tailscale", "ip", "-4"], 15)
    if code == 0 and out.strip():
        return False
    return tailscale_backend_state(runner) not in OWNER_STOPPED_STATES


def tunnel_http_dead(runner: Runner, port: int) -> bool:
    code, out, _ = runner(
        [
            "curl",
            "-sk",
            "-m",
            "6",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            f"https://127.0.0.1:{port}/",
        ],
        10,
    )
    return code != 0 or out.strip() in {"", "000"}


def detect(settings: Mapping[str, str], runner: Runner) -> dict[str, tuple[str, str]]:
    """signal key -> (unit to restart, human reason).  Empty when all is well."""
    signals: dict[str, tuple[str, str]] = {}
    if tailscale_unresponsive(runner):
        signals["tailscaled_unresponsive"] = (
            "tailscaled.service",
            "tailscale cannot report an address",
        )
    port = as_int(settings.get("OPS_TUNNEL_PORT"), 17443)
    if tunnel_held_by(runner, port) == "tailscaled" and tunnel_http_dead(runner, port):
        signals["tunnel_port_held_by_tailscaled"] = (
            "tailscaled.service",
            "review port held by tailscaled and not answering",
        )
    accepts, banner = _tcp_banner(as_int(settings.get("OPS_SSH_PORT"), 22), timeout=5)
    if accepts and not banner:
        signals["sshd_no_banner"] = ("ssh.service", "sshd accepts but sends no banner")
    heal_units = [
        u.strip()
        for u in settings.get("OPS_HEAL_UNITS", ",".join(DEFAULT_HEAL_UNITS)).split(",")
        if u.strip()
    ]
    for unit in heal_units:
        if not unit_allowed(unit):
            continue
        code, out, _ = runner(["systemctl", "is-active", unit], 10)
        if out.strip() == "failed":
            signals[f"failed:{unit}"] = (unit, "unit in failed state")
    return signals


def run_heal(
    settings: Mapping[str, str],
    *,
    runner: Runner = run_command,
    journal: Journal,
    state: StateFile,
    dry_run: bool,
    now: float | None = None,
) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    data = state.load()
    seen: dict[str, int] = {k: int(v) for k, v in dict(data.get("seen", {})).items()}
    last_action: dict[str, float] = {
        k: float(v) for k, v in dict(data.get("last_action", {})).items()
    }
    history = [
        float(t) for t in data.get("history", []) if now - float(t) < HALT_SECONDS
    ]
    halted_at = float(data.get("halted_at", 0) or 0)
    halted = bool(halted_at) and now - halted_at < HALT_SECONDS

    signals = detect(settings, runner)
    # a signal that is gone resets its counter
    seen = {key: seen.get(key, 0) + 1 for key in signals}
    actions: list[dict[str, Any]] = []
    for key, (unit, reason) in signals.items():
        if not unit_allowed(unit):
            journal.write("refused_unit", key=key, unit=unit)
            continue
        if seen[key] < CONFIRM_RUNS:
            journal.write("signal_seen_once", key=key, reason=reason)
            continue
        if halted:
            journal.write("halted", key=key, since=int(halted_at))
            continue
        # cool-down and the hourly count are per SERVICE: two signals that
        # lead to the same restart are one action (finding C5)
        if unit in last_action and now - last_action[unit] < COOLDOWN_SECONDS:
            journal.write("cooldown", key=key, unit=unit)
            continue
        if len([t for t in history if now - t < 3600]) >= MAX_ACTIONS_PER_HOUR:
            journal.write("hourly_cap", key=key, cap=MAX_ACTIONS_PER_HOUR)
            continue
        if len(history) >= MAX_ACTIONS_PER_DAY:
            halted_at, halted = now, True
            journal.write("halted_daily_cap", key=key, cap=MAX_ACTIONS_PER_DAY)
            continue
        record = {"key": key, "unit": unit, "reason": reason, "dry_run": dry_run}
        if dry_run:
            journal.write("would_restart", **record)
        else:
            if key.startswith("failed:"):
                # keep the evidence before `reset-failed` erases it
                _, before, _ = runner(
                    [
                        "systemctl",
                        "show",
                        unit,
                        "-p",
                        "Result",
                        "-p",
                        "ExecMainStatus",
                        "-p",
                        "ExecMainExitTimestamp",
                    ],
                    10,
                )
                journal.write("before_reset_failed", unit=unit, state=before[:300])
                runner(["systemctl", "reset-failed", unit], 20)
            code, _, err = runner(["systemctl", "restart", unit], 60)
            record["ok"] = code == 0
            journal.write("restarted", **record, error=err[-160:])
            last_action[unit] = now
            history.append(now)
        actions.append(record)
    state.save(
        {
            "seen": seen,
            "last_action": last_action,
            "history": history,
            "halted_at": halted_at if halted else 0,
            # the unprivileged beacon cannot see other users' processes
            "tunnel_holder": holder_word(
                tunnel_held_by(runner, as_int(settings.get("OPS_TUNNEL_PORT"), 17443))
            ),
        }
    )
    if not signals:
        journal.write("healthy")
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--settings", type=Path, default=None)
    args = parser.parse_args(argv)
    settings = load_settings(args.settings)
    log_dir = Path(settings.get("OPS_LOG_DIR", str(DEFAULT_LOG_DIR)))
    state_dir = Path(settings.get("OPS_STATE_DIR", str(DEFAULT_STATE_DIR)))
    journal = Journal(log_dir / "self-heal.jsonl")
    dry_run = not args.live and as_dry_run(settings.get("OPS_SELF_HEAL_DRY_RUN"))
    with exclusive_lock(state_dir / "self-heal.lock") as got:
        if not got:
            journal.write("skipped_locked")
            return 0
        run_heal(
            settings,
            journal=journal,
            state=StateFile(state_dir / "self-heal-state.json"),
            dry_run=dry_run,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
