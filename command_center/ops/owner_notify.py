"""Push a short alert to the owner's phone via ntfy.

The fail-closed infra monitor already exits non-zero and writes a JSON line
to the journal; that is invisible without a laptop. This module turns every
condition that needs a human into an HTTP POST to a topic the phone app
subscribes to (https://ntfy.sh protocol). No bot accounts, stdlib only.

Unset ``AICC_OWNER_NTFY_URL`` makes every call a no-op that returns False, so
hosts without a phone channel stay silent without code changes. Open keys
and last-sent times live in a small JSON state file so the same alert is not
resent every tick within the cooldown, and ``resolve`` fires once when the
condition clears.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_DEFAULT_COOLDOWN = 21600
_MESSAGE_LIMIT = 1000
_HTTP_TIMEOUT = 10
_VALID_PRIORITIES = frozenset({"min", "low", "default", "high", "urgent"})


def _state_path() -> Path:
    configured = os.environ.get("AICC_OWNER_NOTIFY_STATE", "").strip()
    if configured:
        return Path(configured)
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "aicc" / "owner-notify.json"
    return Path.home() / ".local" / "state" / "aicc" / "owner-notify.json"


def _cooldown_seconds() -> float:
    raw = os.environ.get("AICC_OWNER_NOTIFY_COOLDOWN", "").strip()
    if not raw:
        return float(_DEFAULT_COOLDOWN)
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return float(_DEFAULT_COOLDOWN)


def _topic_url() -> str | None:
    url = os.environ.get("AICC_OWNER_NTFY_URL", "").strip()
    # Only an http(s) topic URL is a phone channel; file:/custom schemes from a
    # mistyped env value are refused rather than opened.
    if not url.lower().startswith(("https://", "http://")):
        return None
    return url


def _load_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"open": {}}
    if not isinstance(data, dict):
        return {"open": {}}
    open_keys = data.get("open")
    if not isinstance(open_keys, dict):
        return {"open": {}}
    cleaned: dict[str, Any] = {}
    for key, entry in open_keys.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        cleaned[key] = entry
    return {"open": cleaned}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".owner-notify-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def open_keys() -> frozenset[str]:
    """Keys currently remembered as open in the state file."""
    return frozenset(_load_state(_state_path())["open"])


def _post(
    *,
    title: str,
    message: str,
    priority: str,
    tags: tuple[str, ...],
    click_url: str | None,
) -> bool:
    url = _topic_url()
    if url is None:
        return False
    if priority not in _VALID_PRIORITIES:
        priority = "default"
    text = (message or "")[:_MESSAGE_LIMIT]
    headers = {
        "Title": title[:250],
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    token = os.environ.get("AICC_OWNER_NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if tags:
        headers["Tags"] = ",".join(tag for tag in tags if tag)
    if click_url:
        headers["Click"] = click_url
    request = urllib.request.Request(
        url, data=text.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        # Scheme restricted to http(s) in _topic_url.
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # nosec B310
            response.read(1024)
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"owner_notify: POST failed: {exc}", file=sys.stderr)
        return False


def notify(
    key: str,
    title: str,
    message: str,
    *,
    priority: str = "default",
    tags: tuple[str, ...] = (),
    click_url: str | None = None,
) -> bool:
    """Send an alert for ``key``, deduped within the cooldown while open.

    Returns True only when a message was actually delivered. Missing URL,
    cooldown suppression, and network errors all return False and never raise.
    """
    if not key or _topic_url() is None:
        return False
    path = _state_path()
    state = _load_state(path)
    open_map: dict[str, Any] = state["open"]
    now = time.time()
    existing = open_map.get(key)
    if isinstance(existing, dict):
        try:
            last_sent = float(existing.get("last_sent") or 0.0)
        except (TypeError, ValueError):
            last_sent = 0.0
        if now - last_sent < _cooldown_seconds():
            return False
    if not _post(
        title=title,
        message=message,
        priority=priority,
        tags=tags,
        click_url=click_url,
    ):
        return False
    open_map[key] = {"last_sent": now}
    try:
        _atomic_write(path, {"open": open_map})
    except OSError as exc:
        print(f"owner_notify: state write failed: {exc}", file=sys.stderr)
        # The phone already got the message; still report success.
    return True


def resolve(key: str, title: str, message: str) -> bool:
    """Send one 'resolved' message for an open key, then forget it.

    A key that was never open is a no-op returning False. Network errors leave
    the key open so a later tick can retry the resolve.
    """
    if not key or _topic_url() is None:
        return False
    path = _state_path()
    state = _load_state(path)
    open_map: dict[str, Any] = state["open"]
    if key not in open_map:
        return False
    if not _post(
        title=title,
        message=message,
        priority="default",
        tags=("white_check_mark",),
        click_url=None,
    ):
        return False
    open_map.pop(key, None)
    try:
        _atomic_write(path, {"open": open_map})
    except OSError as exc:
        print(f"owner_notify: state write failed: {exc}", file=sys.stderr)
    return True
