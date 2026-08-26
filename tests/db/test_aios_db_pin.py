"""The `aios-db` pin is a real, verifiable pin.

The wheel arrives from an immutable GitHub Release, fetched by the same
verified path as the SDK's and checked against a SHA-256 recorded here. These
tests are about the *lock*, not the download: they run offline and assert that
the thing CI will fetch is fully identified, and that the version AICC pins is
the version its CI installs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "aios-db.lock.json"
WORKFLOW = ROOT / ".github/workflows/ci.yml"

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def test_the_lock_identifies_exactly_one_artifact(lock: dict) -> None:
    assert lock["repository"] == "dimastov-lab/aios"
    assert _SHA1.fullmatch(lock["source_sha"])
    assert _SHA1.fullmatch(lock["accepted_main_sha"])
    assert _SHA256.fullmatch(lock["wheel_sha256"])
    assert lock["release_tag"].startswith("v")
    assert lock["wheel_filename"] == f"aios_db-{lock['version']}-py3-none-any.whl"
    # `api_major` belongs to the SDK: it speaks a wire contract and declares its
    # major. An in-process library has none, and a placeholder would read as a
    # claim that something is compatible with something.
    assert "api_major" not in lock


def test_every_ci_job_installs_the_pinned_wheel(lock: dict) -> None:
    """A job that fetches the SDK but not the DB wheel fails on import, not on
    a clear message — and only in whichever shard happens to touch
    `command_center.db`."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    sdk_fetches = workflow.count("scripts/fetch_aios_sdk_artifact.py --output")
    db_fetches = workflow.count("scripts/fetch_aios_sdk_artifact.py --lock aios-db.lock.json")
    assert db_fetches == sdk_fetches > 0

    assert workflow.count(f"/.artifacts/{lock['wheel_filename']}") == sdk_fetches


def test_the_lock_matches_what_the_boundary_allows() -> None:
    """The pinned distribution is the one the boundary gate allows by name."""
    from tests.architecture import aios_boundary as boundary

    assert boundary.DB_ALLOWED_TOP_LEVEL == "aios_db"
    assert boundary.DB_ADAPTER_PATH == "command_center/db/adapter.py"
    assert (ROOT / boundary.DB_ADAPTER_PATH).is_file()
