"""Status beacon: the host reports its own health where the owner can read it
WITHOUT ssh.

Every ten minutes this publishes one small JSON document (disk, load, key
services, review tunnel, queue shape, age of the last control-loop verdict) to
a dedicated branch of the fleet repository, using the GitHub App installation
token the host already mints (``voyn-aicc-github-token.timer``).  The owner (or
the CTO session) reads it with ``gh api`` from any network -- including the
mobile network that cuts ssh -- and a document that stops updating is itself
the signal "the host is down or cannot reach GitHub".

The repository is PUBLIC, so the document is sanitised twice:

1. it is built from a fixed, allow-listed schema of numbers, booleans and
   short enumerated strings -- no free text from the host ever reaches it;
2. the serialised text is scanned and the publish is REFUSED (fail closed,
   journaled) if it contains anything that looks like an address, a host or
   user path, or a credential.

The branch is rewritten as a single orphan commit each time (blob -> tree ->
parentless commit -> forced ref update), so the repository does not grow by
144 commits a day.  One branch per host role (``ops-status-worker``,
``ops-status-control``) so two hosts never overwrite each other.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from command_center.ops.host_care_common import (
    DEFAULT_LOG_DIR,
    DEFAULT_STATE_DIR,
    Journal,
    Runner,
    StateFile,
    as_bool,
    as_int,
    exclusive_lock,
    gigabytes,
    load_settings,
    run_command,
)
from command_center.ops.disk_guard import free_percent

SCHEMA = 1
ALLOWED_ROLES = {"worker", "control"}
API = "https://api.github.com"
DEFAULT_TOKEN_FILE = Path("/var/lib/aicc/github/token")
#: Units the beacon reports on by default (the owner's settings may narrow it).
DEFAULT_UNITS = (
    "tailscaled",
    "ssh",
    "voyn-aicc-review.timer",
    "voyn-aicc-merge.timer",
    "voyn-aicc-github-token.timer",
    "voyn-aicc-worker-release-deploy.timer",
)

#: pieces of the "a host or user path" pattern; the personal-machine home root
#: is assembled from halves so the literal never sits in this public file
_HOST_PATH_PARTS = ("/home/", "/Us" + "ers/", "/root/", "/srv/", "/var/lib/aicc")
_FORBIDDEN = (
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "ipv4_address"),
    (re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b"), "ipv6_address"),
    (
        re.compile(
            r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|dev|ru|de|app|cloud)\b"
        ),
        "domain_name",
    ),
    (re.compile("|".join(_HOST_PATH_PARTS)), "host_path"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "github_token"),
    (re.compile(r"(?i)BEGIN [A-Z ]*PRIVATE KEY"), "private_key"),
    # A unit NAME may legitimately contain "token" (github-token.timer); what is
    # refused is a credential-shaped word or a JSON key that carries one.
    (re.compile(r"(?i)\b(secret|password|passwd|bearer)\b"), "secret_word"),
    (re.compile(r"(?i)\"[a-z_]*(token|secret|password|key)\"\s*:"), "secret_key"),
    (re.compile(r"(?i)voyn-(worker|control)-\d"), "host_name"),
)


class DirtyStatus(ValueError):
    """The status text would leak something; it is never published."""


def assert_clean(text: str) -> None:
    for pattern, label in _FORBIDDEN:
        if pattern.search(text):
            raise DirtyStatus(label)


def _tcp_banner(port: int, *, timeout: float = 3.0) -> tuple[bool, bool]:
    """(port accepts a connection, a protocol banner arrived)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            try:
                return True, bool(sock.recv(16))
            except OSError:
                return True, False
    except OSError:
        return False, False


def _tcp_open(port: int, *, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _meminfo_available_mb(text: str) -> int | None:
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) // 1024
    return None


def unit_state(unit: str, runner: Runner) -> str:
    code, out, _ = runner(["systemctl", "is-active", unit], 10)
    word = out.strip().splitlines()[0] if out.strip() else ""
    if word in {"active", "inactive", "failed", "activating", "deactivating"}:
        return word
    return "unknown" if code else "active"


def failed_unit_count(runner: Runner) -> int | None:
    code, out, _ = runner(["systemctl", "--failed", "--no-legend", "--plain"], 15)
    if code != 0:
        return None
    return len([line for line in out.splitlines() if line.strip()])


def tunnel_held_by(runner: Runner, port: int) -> str | None:
    """Name of the process that listens on the review tunnel port (only the
    short process name -- never an address or pid).  Callers that PUBLISH it
    reduce it to an enumerated word first (:func:`holder_word`)."""
    code, out, _ = runner(["ss", "-ltnpH"], 10)
    if code != 0:
        return None
    for line in out.splitlines():
        if f":{port} " in line + " " and 'users:(("' in line:
            return line.split('users:(("', 1)[1].split('"', 1)[0][:32]
    return None


def holder_word(name: str | None) -> str | None:
    """Free text from the host never reaches the public document: the holder
    of the review port is one of a fixed set of words."""
    if name is None:
        return None
    return "tailscaled" if name == "tailscaled" else "other"


def queue_shape(repo: str, runner: Runner) -> dict[str, Any] | None:
    code, out, _ = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,mergeStateStatus,labels",
        ],
        40,
    )
    if code != 0:
        return None
    try:
        pulls = json.loads(out or "[]")
    except ValueError:
        return None
    shape = {"open": 0, "behind": 0, "dirty": 0, "window_blocked": 0, "queue_active": 0}
    for pull in pulls:
        shape["open"] += 1
        merge_state = str(pull.get("mergeStateStatus", "")).upper()
        shape["behind"] += merge_state == "BEHIND"
        shape["dirty"] += merge_state == "DIRTY"
        names = {str(label.get("name", "")) for label in pull.get("labels", [])}
        shape["window_blocked"] += "review-window:blocked" in names
        shape["queue_active"] += "queue-active" in names
    code, out, _ = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--limit",
            "1",
            "--json",
            "mergedAt",
        ],
        40,
    )
    if code == 0:
        try:
            merged = json.loads(out or "[]")
            stamp = merged[0]["mergedAt"] if merged else None
            if stamp:
                parsed = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
                shape["last_merge_age_min"] = max(0, int((time.time() - parsed) / 60))
        except (ValueError, KeyError, IndexError, TypeError):
            pass
    return shape


def last_verdict_age_min(runner: Runner, units: list[str], now: float) -> int | None:
    """Minutes since the control loop last logged a verdict or a merge, from
    its own service journal (``MARKER``/``MERGED`` lines)."""
    argv = ["journalctl", "--since", "-24h", "-o", "short-unix", "--no-pager"]
    for unit in units:
        argv += ["-u", unit]
    code, out, _ = runner(argv, 30)
    if code != 0:
        return None
    newest = None
    for line in out.splitlines():
        if "MARKER" not in line and "MERGED" not in line:
            continue
        head = line.split(" ", 1)[0]
        try:
            stamp = float(head)
        except ValueError:
            continue
        newest = stamp if newest is None else max(newest, stamp)
    return None if newest is None else max(0, int((now - newest) / 60))


def collect(
    settings: Mapping[str, str],
    *,
    runner: Runner = run_command,
    now: float | None = None,
    read_text: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Build the allow-listed status document."""
    now = time.time() if now is None else now
    role = settings.get("OPS_HOST_ROLE", "worker")
    if role not in ALLOWED_ROLES:
        role = "worker"
    reader = read_text or (lambda p: Path(p).read_text(encoding="utf-8"))
    disk_path = Path(settings.get("OPS_DISK_PATH", "/"))
    port = as_int(settings.get("OPS_TUNNEL_PORT"), 17443)
    units = [
        u.strip()
        for u in settings.get("OPS_BEACON_UNITS", ",".join(DEFAULT_UNITS)).split(",")
        if u.strip()
    ]

    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "role": role,
        "heartbeat_unix": int(now),
    }
    try:
        import shutil

        usage = shutil.disk_usage(disk_path)
        doc["disk"] = {
            "free_pct": free_percent(disk_path),
            "free_gb": gigabytes(usage.free),
        }
    except OSError:
        doc["disk"] = None
    try:
        doc["load1"] = float(reader("/proc/loadavg").split()[0])
        doc["mem_available_mb"] = _meminfo_available_mb(reader("/proc/meminfo"))
        doc["uptime_days"] = round(float(reader("/proc/uptime").split()[0]) / 86400, 1)
    except (OSError, ValueError, IndexError):
        doc.setdefault("load1", None)
    doc["units"] = {unit: unit_state(unit, runner) for unit in units}
    doc["failed_units"] = failed_unit_count(runner)
    ssh_open, ssh_banner = _tcp_banner(as_int(settings.get("OPS_SSH_PORT"), 22))
    doc["sshd"] = {"accepts": ssh_open, "banner": ssh_banner}
    # The beacon runs unprivileged and cannot name another user's process; the
    # self-heal timer (which can) leaves the holder word in its state file.
    heal_dir = Path(
        settings.get(
            "OPS_HEAL_STATE_DIR", settings.get("OPS_STATE_DIR", str(DEFAULT_STATE_DIR))
        )
    )
    heal_state = StateFile(heal_dir / "self-heal-state.json").load()
    held = holder_word(tunnel_held_by(runner, port)) or heal_state.get("tunnel_holder")
    doc["review_tunnel"] = {
        "listening": _tcp_open(port),
        "held_by": held if held in {"tailscaled", "other"} else None,
    }
    repo = settings.get("OPS_STATUS_REPO", "voyn88/ai-command-center")
    doc["queue"] = queue_shape(repo, runner)
    verdict_units = [
        u.strip()
        for u in settings.get(
            "OPS_VERDICT_UNITS", "voyn-aicc-review.service,voyn-aicc-merge.service"
        ).split(",")
        if u.strip()
    ]
    doc["last_verdict_age_min"] = last_verdict_age_min(runner, verdict_units, now)
    halted_at = float(heal_state.get("halted_at", 0) or 0)
    doc["self_heal_halted"] = bool(halted_at) and now - halted_at < 24 * 3600
    if settings.get("OPS_BEACON_DETAIL", "minimal") == "full":
        return doc
    return minimal_view(doc, as_int(settings.get("OPS_DISK_LOW_FREE_PCT"), 15))


def minimal_view(doc: Mapping[str, Any], low_free_pct: int) -> dict[str, Any]:
    """What a PUBLIC repository may show by default (security review M2): that
    the host is alive, whether it is healthy, how full the disk is and how old
    the control loop's last verdict is -- nothing about which services exist,
    whether ssh is down, or the shape of the queue.

    One deliberate exception to "nothing about the tunnel" (security review of
    PR 1210, requested by the CTO as the first step of moving reviews off the
    owner's Mac): on a worker, ``review_link`` is ``up`` or ``down``.  It shows
    that an external review channel exists and when it is unreachable; it is a
    fixed word, never an address, port or reason, and it does not count as a
    degraded reason.  On any other host the key is absent."""
    reasons = 0
    disk = doc.get("disk") or {}
    free = disk.get("free_pct")
    if isinstance(free, (int, float)) and free < low_free_pct:
        reasons += 1
    reasons += sum(1 for v in (doc.get("units") or {}).values() if v != "active")
    reasons += 1 if (doc.get("failed_units") or 0) > 0 else 0
    sshd = doc.get("sshd") or {}
    reasons += 1 if not sshd.get("banner") else 0
    reasons += (
        1 if (doc.get("review_tunnel") or {}).get("held_by") == "tailscaled" else 0
    )
    reasons += 1 if doc.get("self_heal_halted") else 0
    view: dict[str, Any] = {
        "schema": SCHEMA,
        "role": doc.get("role"),
        "heartbeat_unix": doc.get("heartbeat_unix"),
        "status": "ok" if reasons == 0 else "degraded",
        "degraded_count": reasons,
        "disk_free_pct": free,
        "last_verdict_age_min": doc.get("last_verdict_age_min"),
    }
    link = review_link_word(doc)
    if link is not None:
        view["review_link"] = link
    return view


def review_link_word(doc: Mapping[str, Any]) -> str | None:
    """Is the external review channel up?  ``up`` / ``down`` on a worker (its
    loopback is where the tunnel ends), ``None`` elsewhere.  A fixed word, no
    address; it does not count as a degraded reason: after the Mac path is
    switched off, ``down`` is the expected state."""
    if doc.get("role") != "worker":
        return None
    listening = (doc.get("review_tunnel") or {}).get("listening")
    if listening is None:
        return None
    return "up" if listening else "down"


def render(doc: Mapping[str, Any]) -> str:
    text = json.dumps(doc, indent=2, sort_keys=True)
    assert_clean(text)
    return text + "\n"


# --------------------------------------------------------------------------
# publishing (GitHub git-data API; injectable opener for tests)

Opener = Callable[[urllib.request.Request], Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """``urllib`` re-sends the Authorization header across a redirect; the API
    host never redirects a git-data call, so any redirect is refused
    (finding M5)."""

    def redirect_request(self, *args: Any, **kwargs: Any):
        return None


def _default_open(request: urllib.request.Request) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=30)


def _call(
    opener: Opener, token: str, method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "voyn-ops-beacon",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with opener(request) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        return exc.code, payload


def publish_branch(
    repo: str,
    branch: str,
    text: str,
    token: str,
    *,
    opener: Opener = _default_open,
) -> str:
    """Replace ``branch`` with one parentless commit holding ``status.json``.
    Returns the new commit sha; raises RuntimeError on any API refusal."""
    assert_clean(text)
    base = f"/repos/{repo}/git"
    code, blob = _call(
        opener, token, "POST", f"{base}/blobs", {"content": text, "encoding": "utf-8"}
    )
    if code != 201:
        raise RuntimeError(f"blob:{code}")
    code, tree = _call(
        opener,
        token,
        "POST",
        f"{base}/trees",
        {
            "tree": [
                {
                    "path": "status.json",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            ]
        },
    )
    if code != 201:
        raise RuntimeError(f"tree:{code}")
    code, commit = _call(
        opener,
        token,
        "POST",
        f"{base}/commits",
        {"message": "ops status", "tree": tree["sha"], "parents": []},
    )
    if code != 201:
        raise RuntimeError(f"commit:{code}")
    ref = f"refs/heads/{branch}"
    code, _ = _call(opener, token, "GET", f"{base}/{ref}")
    if code == 200:
        code, _ = _call(
            opener,
            token,
            "PATCH",
            f"{base}/{ref}",
            {"sha": commit["sha"], "force": True},
        )
        if code != 200:
            raise RuntimeError(f"ref_update:{code}")
    else:
        code, _ = _call(
            opener, token, "POST", f"{base}/refs", {"ref": ref, "sha": commit["sha"]}
        )
        if code != 201:
            raise RuntimeError(f"ref_create:{code}")
    return str(commit["sha"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--print", action="store_true", help="print, do not publish")
    parser.add_argument("--settings", type=Path, default=None)
    args = parser.parse_args(argv)
    settings = load_settings(args.settings)
    log_dir = Path(settings.get("OPS_LOG_DIR", str(DEFAULT_LOG_DIR)))
    state_dir = Path(settings.get("OPS_STATE_DIR", str(DEFAULT_STATE_DIR)))
    journal = Journal(log_dir / "beacon.jsonl")
    with exclusive_lock(state_dir / "beacon.lock") as got:
        if not got:
            journal.write("skipped_locked")
            return 0
        doc = collect(settings)
        try:
            text = render(doc)
        except DirtyStatus as exc:
            journal.write("refused_dirty_status", why=str(exc))
            return 0
        StateFile(state_dir / "status.json").save(doc)
        if args.print:
            print(text)
            return 0
        if not as_bool(settings.get("OPS_BEACON_PUBLISH"), True):
            journal.write("publish_disabled")
            return 0
        token_file = Path(
            settings.get("OPS_GITHUB_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))
        )
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            journal.write("publish_skipped", why="no_token_file")
            return 0
        if not token:
            journal.write("publish_skipped", why="empty_token")
            return 0
        role = doc["role"]
        try:
            sha = publish_branch(
                settings.get("OPS_STATUS_REPO", "voyn88/ai-command-center"),
                f"ops-status-{role}",
                text,
                token,
            )
            journal.write("published", branch=f"ops-status-{role}", commit=sha[:12])
        except (RuntimeError, OSError, urllib.error.URLError) as exc:
            journal.write("publish_failed", why=str(exc)[:80])
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
