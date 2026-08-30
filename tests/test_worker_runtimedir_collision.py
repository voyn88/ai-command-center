"""Regression guard for VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION.

worker-01 ran three units -- voyn-aicc-worker.service, voyn-aicc-worker@3 and
voyn-aicc-worker@4 -- that all declared `RuntimeDirectory=voyn-aicc-worker`,
one shared path. systemd does not refcount RuntimeDirectory across units:
whenever any one of them stopped, systemd deleted that directory out from
under the still-running siblings, wiping their PGPASSFILE credential
mid-flight. That produced psycopg PoolTimeouts, worker exit(1), burned
attempts and 594 dead-lettered tasks over 2026-08-27..29.

The versioned template unit (`voyn-aicc-worker@.service`) fixes this by
keying its RuntimeDirectory off `%i` so each instantiated lane gets its own
subdirectory. The units that caused the incident were hand-made host copies,
never committed here -- so nothing in the repository's history would have
caught them. This module makes the invariant structural: it fails if any
committed unit reintroduces a RuntimeDirectory root shared with another unit,
or if the worker template regresses to a non-per-instance path.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYSTEMD_DIR = ROOT / "deploy" / "systemd"

_RUNTIME_DIRECTORY = re.compile(r"^RuntimeDirectory=(.+)$", re.MULTILINE)


def _runtime_directories() -> dict[str, str]:
    found = {}
    for path in sorted(SYSTEMD_DIR.glob("*.service")):
        match = _RUNTIME_DIRECTORY.search(path.read_text())
        if match:
            found[path.name] = match.group(1).strip()
    return found


def test_templated_worker_runtime_directory_is_per_instance() -> None:
    """voyn-aicc-worker@.service must key its RuntimeDirectory off %i.

    A literal path with no %i/%I placeholder is shared verbatim by every
    instantiated lane -- exactly the collision that deleted a running lane's
    credential directory out from under it.
    """
    value = _runtime_directories()["voyn-aicc-worker@.service"]
    assert "%i" in value or "%I" in value, (
        "voyn-aicc-worker@.service RuntimeDirectory must be keyed by instance "
        f"name (%i); got {value!r}, which every lane would share"
    )


def test_no_two_units_share_a_runtime_directory_root() -> None:
    """No two committed unit files may root their RuntimeDirectory at the same path.

    systemd deletes a unit's RuntimeDirectory on stop regardless of what else
    is using it. Two unit *files* rooted at the same top-level /run path --
    templated or not -- means stopping one can delete the credential
    directory of a lane still served by the other.
    """
    roots: dict[str, str] = {}
    for name, value in _runtime_directories().items():
        root = value.split("/", 1)[0]
        owner = roots.get(root)
        assert owner is None, (
            f"{name!r} and {owner!r} both root RuntimeDirectory at {root!r} -- "
            "stopping either one deletes the other's live credential directory "
            "(this is the VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION defect)"
        )
        roots[root] = name
