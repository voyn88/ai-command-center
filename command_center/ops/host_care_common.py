"""Shared plumbing for the host self-care timers (disk guard, status beacon,
self-heal).

Why this exists: the owner has no reliable shell on the fleet hosts (the
mobile network cuts SSH and Tailscale, root is only in the provider's web
console), so the hosts must look after themselves and report on their own.
Three small timers do that; this module holds what they share so each of them
stays short and reviewable:

- a host-local settings file (``/etc/aicc/ops-autonomy.env``) -- the user
  home paths, the owner's account name and the clone/cache roots live THERE
  and not in the repository (this is a public repository; its leak guard
  refuses home paths).  Generic deployment paths that other deploy files
  already publish (the worker lane workspaces, the fleet token store) do
  appear in code and units;
- one JSON-lines journal per timer, one object per decision;
- an exclusive lock so two runs of the same timer never overlap;
- a tiny state file for "seen twice" and cool-down bookkeeping.

Nothing here talks to the network and nothing here deletes anything.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS = Path("/etc/aicc/ops-autonomy.env")
DEFAULT_LOG_DIR = Path("/var/log/voyn-ops")
DEFAULT_STATE_DIR = Path("/var/lib/voyn-ops")

#: A command runner: argv, timeout -> (returncode, stdout, stderr). Tests
#: replace it; production uses :func:`run_command`.
Runner = Callable[[Sequence[str], int], tuple[int, str, str]]


def run_command(argv: Sequence[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run ``argv`` without a shell; a missing binary or a timeout is a
    non-zero result, never an exception (a self-care timer must not die on the
    very fault it is looking for)."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {argv[0]}"
    return done.returncode, done.stdout, done.stderr


def parse_settings(text: str) -> dict[str, str]:
    """``KEY=value`` lines, ``#`` comments, optional single/double quotes."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_settings(
    path: Path | None = None, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Settings file first, process environment on top (the unit can override
    one value without editing the file)."""
    merged: dict[str, str] = {}
    target = path or Path(os.environ.get("AICC_OPS_SETTINGS", str(DEFAULT_SETTINGS)))
    try:
        merged.update(parse_settings(target.read_text(encoding="utf-8")))
    except OSError:
        pass
    env = os.environ if environ is None else environ
    for key, value in env.items():
        if key.startswith("OPS_"):
            merged[key] = value
    return merged


def as_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_EXPLICIT_LIVE = {"0", "false", "no", "off"}
_EXPLICIT_DRY = {"1", "true", "yes", "on"}


def as_dry_run(value: str | None) -> bool:
    """Dry run unless the setting EXPLICITLY says live.

    Security review finding N1: the first version treated only ``1/true/yes/
    on`` as dry run, so a typo typed in the web console (``y``, ``dry``,
    ``enabled``, a stray character) silently armed real deletion.  Now a
    destructive mode needs one of ``0/false/no/off``; every other value,
    including an empty or unknown one, stays a dry run.
    """
    if value is None:
        return True
    word = value.strip().lower()
    if word in _EXPLICIT_LIVE:
        return False
    return True


def dry_run_value_is_unclear(value: str | None) -> bool:
    """True for a value that is neither an explicit dry nor an explicit live
    word (journaled so the typo is visible)."""
    if value is None or value == "":
        return False
    word = value.strip().lower()
    return word not in _EXPLICIT_LIVE and word not in _EXPLICIT_DRY


def as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except TypeError, ValueError:
        return default


def as_paths(value: str | None) -> list[Path]:
    """Colon-separated absolute paths; relative or empty entries are dropped
    (a relative root would make every later safety check ambiguous)."""
    out: list[Path] = []
    for part in (value or "").split(":"):
        part = part.strip()
        if part.startswith("/"):
            out.append(Path(part))
    return out


class Journal:
    """Append-only JSON-lines decision log; also mirrored to stdout so
    ``journalctl -u <unit>`` shows the same lines."""

    def __init__(self, path: Path | None, *, echo: Callable[[str], None] | None = None):
        self.path = path
        self._echo = echo if echo is not None else print

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"ts": int(time.time()), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        self._echo(line)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # The stdout copy above is already in the unit journal.
                pass
        return record

    #: the journal never grows past roughly this many bytes (one generation of
    #: history is kept as ``<name>.1``)
    MAX_BYTES = 5_000_000

    def _rotate(self) -> None:
        assert self.path is not None
        try:
            if self.path.stat().st_size > self.MAX_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except OSError:
            pass


@contextmanager
def exclusive_lock(path: Path) -> Iterator[bool]:
    """Non-blocking flock; yields False when another run holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@dataclass
class StateFile:
    """Small JSON dict persisted between runs (atomic replace)."""

    path: Path

    def load(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def save(self, data: Mapping[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


def gigabytes(size_bytes: int) -> float:
    return round(size_bytes / 1_000_000_000, 2)
