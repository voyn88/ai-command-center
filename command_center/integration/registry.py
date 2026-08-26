"""Project registry — the data model of the Integration Center.

Single writer of ``data/integration_registry.json`` (+ its lock file), per
``docs/AUTHORITY_MAP.md`` and ``docs/INTEGRATION_CENTER.md``. Operator
configuration (machine-local repo paths, remotes), not execution truth —
which is why this is a JSON dict store in the style of
``project_config.json`` and deliberately *not* a ``runtime.db`` table.

Every write goes through ``storage.file_lock`` + ``storage.atomic_write_json``
(lock → reload fresh → mutate → atomic replace), so a concurrent writer's
change is never clobbered by a stale in-memory snapshot — the same rule
``tasks_repository``/``project_config`` follow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from command_center import models, storage

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = storage.resolve_data_dir(ROOT)
REGISTRY_FILE = DATA_DIR / "integration_registry.json"
REGISTRY_LOCK_FILE = DATA_DIR / "integration_registry.lock"

#: Registry-entry kinds (see docs/INTEGRATION_CENTER.md — data model).
ENTRY_KINDS: tuple[str, ...] = (
    "application",
    "service",
    "library",
    "infrastructure",
    "other",
)

#: Generic placeholder entries, seeded on first read so the Projects page has
#: something to show before the operator configures anything real. The actual
#: registry contents are machine-local runtime data
#: (`data/integration_registry.json`, gitignored) — real project names, paths
#: and remotes are configuration and are never committed to this repository.
DEFAULT_ENTRIES: list[dict[str, Any]] = [
    {
        "id": "example-app",
        "name": "Example App",
        "kind": "application",
        "project": "AICC",
        "repo_path": None,
        "remote": None,
        "default_branch": "main",
    },
    {
        "id": "example-lib",
        "name": "Example Library",
        "kind": "library",
        "project": "PERSONAL",
        "repo_path": None,
        "remote": None,
        "default_branch": "main",
    },
]

_ENTRY_FIELDS = frozenset(
    {"id", "name", "kind", "project", "repo_path", "remote", "default_branch"}
)


class RegistryValidationError(ValueError):
    """A registry entry violates the documented data model."""


def normalize_entry(entry: dict) -> dict:
    """Validate + normalize one registry entry against the documented model."""
    unknown = set(entry) - _ENTRY_FIELDS
    if unknown:
        raise RegistryValidationError(f"Unknown registry fields: {sorted(unknown)}")
    entry_id = (entry.get("id") or "").strip()
    if not entry_id:
        raise RegistryValidationError("Registry entry requires a non-empty id")
    kind = entry.get("kind") or "other"
    if kind not in ENTRY_KINDS:
        raise RegistryValidationError(
            f"Unknown kind {kind!r} (allowed: {', '.join(ENTRY_KINDS)})"
        )
    project = entry.get("project")
    if project not in models.PROJECT_IDS:
        raise RegistryValidationError(
            f"project {project!r} is not one of models.PROJECT_IDS — the "
            "registry joins onto the task board through that namespace"
        )
    return {
        "id": entry_id,
        "name": (entry.get("name") or entry_id).strip(),
        "kind": kind,
        "project": project,
        "repo_path": entry.get("repo_path") or None,
        "remote": entry.get("remote") or None,
        "default_branch": (entry.get("default_branch") or "main").strip(),
    }


def _read_raw() -> list[dict]:
    data = storage.read_json(REGISTRY_FILE, default=None)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return []
    return [e for e in data["entries"] if isinstance(e, dict)]


def load_entries() -> list[dict]:
    """All registry entries, seeded with the defaults on first read.

    Seeding writes through the same locked path as any other write, so two
    concurrent first reads cannot both write the file.
    """
    entries = _read_raw()
    if entries:
        return [normalize_entry(e) for e in entries]
    with storage.file_lock(REGISTRY_LOCK_FILE):
        entries = _read_raw()  # reload under the lock — another writer may have seeded
        if not entries:
            entries = [normalize_entry(e) for e in DEFAULT_ENTRIES]
            storage.atomic_write_json(REGISTRY_FILE, {"entries": entries})
    return [normalize_entry(e) for e in entries]


def get_entry(entry_id: str) -> dict | None:
    return next((e for e in load_entries() if e["id"] == entry_id), None)


def upsert_entry(entry: dict) -> dict:
    """Locked create-or-replace by ``id`` — the only mutation this store has.

    Reloads fresh under the lock before writing (never persists a stale
    snapshot handed in by a caller).
    """
    normalized = normalize_entry(entry)
    with storage.file_lock(REGISTRY_LOCK_FILE):
        entries = [normalize_entry(e) for e in _read_raw()] or [
            normalize_entry(e) for e in DEFAULT_ENTRIES
        ]
        replaced = False
        for index, existing in enumerate(entries):
            if existing["id"] == normalized["id"]:
                entries[index] = normalized
                replaced = True
                break
        if not replaced:
            entries.append(normalized)
        storage.atomic_write_json(REGISTRY_FILE, {"entries": entries})
    return normalized
