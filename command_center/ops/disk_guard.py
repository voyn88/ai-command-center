"""Disk guard: free space on a fleet host without a human at the keyboard.

The worker host filled to 97 % and nobody who could clean it had a working
shell (the mobile network cuts SSH/Tailscale; root is only in the provider's
web console).  This timer watches free space and, below a threshold, removes
things that are provably regenerable, in the order that costs least:

1. systemd journal beyond a size cap,
2. package caches (npm, pip, uv) -- the configured cache directories only,
3. orphaned model blobs of the local model server (blobs no manifest names),
4. per-task clones whose pull request is MERGED or CLOSED for at least
   ``min_age_days`` and that nothing is using.

It stops as soon as the target free percentage is projected/reached.

What it NEVER touches (the point of the design):

- a clone with an OPEN pull request, with no pull request at all, whose branch
  is the default branch or unknown, that a process still has as its working
  directory, that was modified inside the age window, or that carries a
  ``.aicc-keep`` marker;
- backups, the database, secrets, state: any path containing one of the
  protected tokens is refused, and clones are only ever deleted as DIRECT
  children of a configured root (a symlink or a path escaping the root is
  refused);
- anything outside the roots/cache directories the owner listed in the
  host-local settings file.

Dry run is the default: it journals what it WOULD delete and how many GB.
Live deletion needs ``OPS_DISK_GUARD_DRY_RUN=0`` in the host settings, and is
still capped per run (``max_clones``, ``max_delete_gb``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from functools import partial
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from command_center.ops.autonomy_common import (
    DEFAULT_LOG_DIR,
    DEFAULT_STATE_DIR,
    Journal,
    Runner,
    as_bool,
    as_int,
    as_paths,
    exclusive_lock,
    gigabytes,
    load_settings,
    run_command,
)

#: A path containing any of these is never deleted, whatever else says it may.
PROTECTED_TOKENS = (
    "backup",
    "continuity",
    "pgdata",
    "postgres",
    "secret",
    ".ssh",
    ".gnupg",
    "credential",
    "/etc/",
    "aicc-keep",
)
KEEP_MARKER = ".aicc-keep"
DEFAULT_BRANCHES = {"main", "master", "HEAD", ""}
_DAY = 86400


@dataclass
class Candidate:
    kind: str  # journal | cache | ollama_blob | clone
    path: str
    size: int
    reason: str
    action: Callable[[], bool] | None = field(default=None, repr=False)


@dataclass
class Config:
    target_path: Path
    low_free_pct: int
    target_free_pct: int
    min_age_days: int
    clone_roots: list[Path]
    cache_dirs: list[Path]
    ollama_models: Path | None
    journal_max_mb: int
    max_clones: int
    max_delete_gb: int
    dry_run: bool
    log_file: Path | None
    lock_file: Path

    @classmethod
    def from_settings(cls, values: Mapping[str, str]) -> Config:
        models = values.get("OPS_OLLAMA_MODELS_DIR", "").strip()
        return cls(
            target_path=Path(values.get("OPS_DISK_PATH", "/")),
            low_free_pct=as_int(values.get("OPS_DISK_LOW_FREE_PCT"), 15),
            target_free_pct=as_int(values.get("OPS_DISK_TARGET_FREE_PCT"), 25),
            min_age_days=max(1, as_int(values.get("OPS_CLONE_MIN_AGE_DAYS"), 3)),
            clone_roots=as_paths(values.get("OPS_CLONE_ROOTS")),
            cache_dirs=as_paths(values.get("OPS_CACHE_DIRS")),
            ollama_models=Path(models) if models.startswith("/") else None,
            journal_max_mb=as_int(values.get("OPS_JOURNAL_MAX_MB"), 500),
            max_clones=as_int(values.get("OPS_DISK_MAX_CLONES"), 25),
            max_delete_gb=as_int(values.get("OPS_DISK_MAX_GB"), 40),
            dry_run=as_bool(values.get("OPS_DISK_GUARD_DRY_RUN"), True),
            log_file=Path(
                values.get("OPS_DISK_LOG", str(DEFAULT_LOG_DIR / "disk-guard.jsonl"))
            ),
            lock_file=Path(
                values.get("OPS_DISK_LOCK", str(DEFAULT_STATE_DIR / "disk-guard.lock"))
            ),
        )


def free_percent(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return round(usage.free * 100.0 / usage.total, 2) if usage.total else 0.0


def dir_size(path: Path) -> int:
    """Bytes under ``path``; symlinks are counted as themselves, never followed."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def is_protected(path: Path) -> bool:
    text = str(path).lower()
    return any(token in text for token in PROTECTED_TOKENS)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError, OSError:
        return False
    return True


def safe_remove_tree(path: Path, root: Path) -> bool:
    """Delete ``path`` only if it is a real directory that is a DIRECT child of
    ``root``; anything else is refused."""
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        if path.parent.resolve() != root.resolve() or path.resolve() == root.resolve():
            return False
        if is_protected(path):
            return False
        shutil.rmtree(path)
    except OSError:
        return False
    return True


def newest_mtime(path: Path) -> float:
    """Newest modification time among the directory, its git metadata and its
    top level (cheap activity proxy; never walks the whole tree)."""
    newest = 0.0
    probes = [
        path,
        path / ".git",
        path / ".git" / "HEAD",
        path / ".git" / "index",
        path / ".git" / "FETCH_HEAD",
    ]
    try:
        probes.extend(Path(entry.path) for entry in os.scandir(path))
    except OSError:
        pass
    for probe in probes:
        try:
            newest = max(newest, probe.lstat().st_mtime)
        except OSError:
            continue
    return newest


def process_cwds(proc_root: Path = Path("/proc")) -> list[str]:
    """Working directories of every visible process."""
    cwds: list[str] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return cwds
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cwds.append(os.readlink(entry / "cwd"))
        except OSError:
            continue
    return cwds


def in_use(path: Path, cwds: Iterable[str]) -> bool:
    prefix = str(path.resolve())
    return any(cwd == prefix or cwd.startswith(prefix + "/") for cwd in cwds)


_SLUG = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def repo_slug(remote_url: str) -> str | None:
    match = _SLUG.search(remote_url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _git(path: Path, args: list[str], runner: Runner) -> str | None:
    code, out, _ = runner(["git", "-C", str(path), *args], 20)
    return out.strip() if code == 0 else None


def clone_verdict(
    path: Path,
    *,
    now: float,
    min_age_days: int,
    cwds: list[str],
    runner: Runner,
) -> tuple[bool, str]:
    """(deletable, reason).  Deletable only when every check passes."""
    if (path / KEEP_MARKER).exists():
        return False, "keep_marker"
    if now - newest_mtime(path) < min_age_days * _DAY:
        return False, "modified_inside_age_window"
    if in_use(path, cwds):
        return False, "process_working_directory"
    if _git(path, ["rev-parse", "--git-dir"], runner) is None:
        return False, "not_a_git_clone"
    branch = _git(path, ["rev-parse", "--abbrev-ref", "HEAD"], runner) or ""
    if branch in DEFAULT_BRANCHES:
        return False, "default_or_unknown_branch"
    remote = _git(path, ["remote", "get-url", "origin"], runner) or ""
    slug = repo_slug(remote)
    if slug is None:
        return False, "no_github_remote"
    code, out, _ = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "number,state,mergedAt,closedAt",
        ],
        40,
    )
    if code != 0:
        return False, "gh_unavailable"
    try:
        pulls = json.loads(out or "[]")
    except ValueError:
        return False, "gh_unreadable"
    if not isinstance(pulls, list) or not pulls:
        return False, "no_pull_request"
    latest = 0.0
    for pull in pulls:
        state = str(pull.get("state", "")).upper()
        if state == "OPEN":
            return False, "open_pull_request"
        stamp = pull.get("mergedAt") or pull.get("closedAt")
        if not stamp:
            return False, "closed_without_time"
        try:
            parsed = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return False, "unreadable_time"
        # mktime is local time; the offset is irrelevant at day granularity.
        latest = max(latest, parsed)
    if now - latest < min_age_days * _DAY:
        return False, "pull_request_closed_inside_age_window"
    return True, "pull_request_merged_or_closed"


def plan_clones(
    cfg: Config, *, now: float, runner: Runner, cwds: list[str]
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    candidates: list[Candidate] = []
    skipped: list[dict[str, Any]] = []
    for root in cfg.clone_roots:
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink() or is_protected(child):
                skipped.append({"path": str(child), "why": "symlink_or_protected"})
                continue
            ok, reason = clone_verdict(
                child,
                now=now,
                min_age_days=cfg.min_age_days,
                cwds=cwds,
                runner=runner,
            )
            if not ok:
                skipped.append({"path": str(child), "why": reason})
                continue
            candidates.append(
                Candidate(
                    kind="clone",
                    path=str(child),
                    size=dir_size(child),
                    reason=reason,
                    action=partial(safe_remove_tree, child, root),
                )
            )
    candidates.sort(key=lambda c: newest_mtime(Path(c.path)))  # oldest first
    return candidates, skipped


def plan_caches(cfg: Config, *, now: float) -> list[Candidate]:
    out: list[Candidate] = []
    for cache in cfg.cache_dirs:
        if is_protected(cache) or not cache.is_dir() or cache.is_symlink():
            continue
        size = dir_size(cache)
        if size <= 0:
            continue

        def clear(target: Path = cache) -> bool:
            ok = True
            for child in list(target.iterdir()):
                try:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except OSError:
                    ok = False
            return ok

        out.append(Candidate("cache", str(cache), size, "package_cache", action=clear))
    return out


def referenced_blobs(models_dir: Path) -> set[str] | None:
    """Digest file names every manifest names; ``None`` when the manifests
    cannot be read with certainty (then NOTHING is treated as orphaned)."""
    manifests = models_dir / "manifests"
    if not manifests.is_dir():
        return None
    names: set[str] = set()
    seen = 0
    for path in manifests.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return None
        layers = list(data.get("layers", [])) + [data.get("config", {})]
        for layer in layers:
            digest = str((layer or {}).get("digest", ""))
            if digest.startswith("sha256:"):
                names.add("sha256-" + digest.split(":", 1)[1])
        seen += 1
    return names if seen else None


def plan_ollama_orphans(cfg: Config, *, now: float) -> list[Candidate]:
    if cfg.ollama_models is None or is_protected(cfg.ollama_models):
        return []
    referenced = referenced_blobs(cfg.ollama_models)
    blobs = cfg.ollama_models / "blobs"
    if referenced is None or not blobs.is_dir():
        return []
    out: list[Candidate] = []
    for blob in sorted(blobs.iterdir()):
        if not blob.is_file() or blob.is_symlink():
            continue
        if not blob.name.startswith("sha256-") or blob.name in referenced:
            continue
        try:
            stat = blob.stat()
        except OSError:
            continue
        if now - stat.st_mtime < _DAY:
            continue  # a pull in progress writes new blobs first
        out.append(
            Candidate(
                "ollama_blob",
                str(blob),
                stat.st_size,
                "no_manifest_references_it",
                action=partial(_unlink, blob),
            )
        )
    return out


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def run_guard(
    cfg: Config,
    *,
    runner: Runner = run_command,
    journal: Journal,
    now: float | None = None,
    cwds: list[str] | None = None,
    free_pct: Callable[[Path], float] = free_percent,
) -> dict[str, Any]:
    """One pass.  Returns the summary dict (also journaled)."""
    now = time.time() if now is None else now
    before = free_pct(cfg.target_path)
    summary: dict[str, Any] = {
        "free_pct_before": before,
        "low_free_pct": cfg.low_free_pct,
        "dry_run": cfg.dry_run,
        "acted": False,
        "removed": [],
        "would_remove": [],
        "freed_gb": 0.0,
    }
    if before >= cfg.low_free_pct:
        journal.write("disk_ok", **summary)
        return summary

    total = shutil.disk_usage(cfg.target_path).total or 1
    # derived from the same reading the threshold used (one source of truth)
    projected_free = total * before / 100.0
    budget_bytes = cfg.max_delete_gb * 1_000_000_000
    freed = 0
    clones_done = 0
    target_bytes = total * cfg.target_free_pct / 100.0

    def want_more() -> bool:
        return projected_free < target_bytes and freed < budget_bytes

    # 1. journal -------------------------------------------------------------
    if want_more() and cfg.journal_max_mb > 0:
        if cfg.dry_run:
            journal.write("would_vacuum_journal", max_mb=cfg.journal_max_mb)
        else:
            code, _, err = runner(
                ["journalctl", f"--vacuum-size={cfg.journal_max_mb}M"], 60
            )
            journal.write("vacuum_journal", ok=code == 0, error=err[-200:])
        summary["acted"] = True

    # 2-4. candidates ---------------------------------------------------------
    candidates: list[Candidate] = []
    candidates += plan_caches(cfg, now=now)
    candidates += plan_ollama_orphans(cfg, now=now)
    clone_list, skipped = plan_clones(
        cfg,
        now=now,
        runner=runner,
        cwds=process_cwds() if cwds is None else cwds,
    )
    candidates += clone_list
    journal.write("skipped_clones", count=len(skipped), sample=skipped[:15])

    for cand in candidates:
        if not want_more():
            break
        if cand.kind == "clone" and clones_done >= cfg.max_clones:
            journal.write("clone_cap_reached", cap=cfg.max_clones)
            continue
        entry = {
            "kind": cand.kind,
            "path": cand.path,
            "gb": gigabytes(cand.size),
            "reason": cand.reason,
        }
        if cfg.dry_run:
            summary["would_remove"].append(entry)
            journal.write("would_remove", **entry)
            ok = True
        else:
            ok = bool(cand.action and cand.action())
            summary["removed" if ok else "failed"] = summary.get(
                "removed" if ok else "failed", []
            ) + [entry]
            journal.write("removed" if ok else "remove_failed", **entry)
        if ok:
            summary["acted"] = True
            projected_free += cand.size
            freed += cand.size
            if cand.kind == "clone":
                clones_done += 1

    summary["freed_gb"] = gigabytes(freed)
    summary["free_pct_after"] = (
        round(projected_free * 100.0 / total, 2)
        if cfg.dry_run
        else free_pct(cfg.target_path)
    )
    journal.write("disk_guard_done", **summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--live",
        action="store_true",
        help="really delete (overrides the dry-run default for this run only)",
    )
    parser.add_argument("--settings", type=Path, default=None)
    args = parser.parse_args(argv)
    values = load_settings(args.settings)
    cfg = Config.from_settings(values)
    if args.live:
        cfg.dry_run = False
    journal = Journal(cfg.log_file)
    with exclusive_lock(cfg.lock_file) as got:
        if not got:
            journal.write("skipped_locked")
            return 0
        run_guard(cfg, journal=journal)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
