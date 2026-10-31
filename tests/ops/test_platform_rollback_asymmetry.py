"""Pins for VOYN-W0-AICC-SRV-09-PLATFORM-ROLLBACK-ASYMMETRY.

The finding is that an AICC DB restore cannot undo platform writes AICC made
through ``voyn-lease`` and ``aios_sdk``. These checks keep the inventory and
the pre-restore capture script honest without needing a live platform DB.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE = REPO_ROOT / "scripts" / "aicc_pg_restore.sh"
DOC = REPO_ROOT / "docs" / "operations" / "PLATFORM_ROLLBACK_ASYMMETRY.md"
MANIFEST_SCRIPT = REPO_ROOT / "scripts" / "aicc_pre_restore_platform_manifest.py"

# The only AICC call sites that write into the AIOS platform database.
# worktree_lease.py is deliberately absent: it only runs `voyn-lease list`.
_LEASE_WRITE_SITES = {
    REPO_ROOT / "command_center" / "worker" / "writer_lease.py": frozenset(
        {"acquire", "release"}
    ),
    REPO_ROOT / "command_center" / "orchestrator" / "publish.py": frozenset(
        {"acquire", "install-hooks", "release"}
    ),
}

_TASK_GATEWAY = REPO_ROOT / "command_center" / "application" / "aios_tasks.py"
_TASK_WRITE_METHODS = frozenset(
    {"create_task", "assign_task", "start_task", "complete_task", "cancel_task"}
)


def _load_manifest_module():
    spec = importlib.util.spec_from_file_location(
        "aicc_pre_restore_platform_manifest", MANIFEST_SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _string_constants(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_restore_script_points_at_the_asymmetry_procedure() -> None:
    text = RESTORE.read_text(encoding="utf-8")
    assert "docs/operations/PLATFORM_ROLLBACK_ASYMMETRY.md" in text
    assert "repo_lease" in text
    assert "aios_sdk" in text


def test_asymmetry_doc_names_both_platform_write_surfaces_and_lease_split() -> None:
    text = DOC.read_text(encoding="utf-8")
    for needle in (
        "repo_lease",
        "repo_lease_event",
        "tasks",
        "task_events",
        "voyn-lease",
        "aios_sdk",
        "SRV-02-LEASE-SPLIT",
        "aicc_pre_restore_platform_manifest.py",
        "WorkQueueReadStore",
        "GET /api/v1/queue/items?state=claimed",
    ):
        assert needle in text, f"procedure doc missing {needle!r}"


def test_lease_write_call_sites_still_match_the_inventory() -> None:
    for path, verbs in _LEASE_WRITE_SITES.items():
        found = _string_constants(path) & {"acquire", "install-hooks", "release", "list"}
        assert verbs <= found, f"{path.name} missing lease verbs {verbs - found}"
        assert "list" not in found or path.name != "writer_lease.py"


def test_worktree_lease_is_list_only() -> None:
    path = REPO_ROOT / "command_center" / "worker" / "worktree_lease.py"
    found = _string_constants(path) & {"acquire", "install-hooks", "release", "list"}
    assert found == {"list"}


def test_task_gateway_still_owns_the_only_sdk_write_verbs() -> None:
    tree = ast.parse(_TASK_GATEWAY.read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.endswith("_task")
    }
    # create/assign/start/complete/cancel are the mutating surface; get_task
    # is read-only and deliberately outside this pin.
    assert _TASK_WRITE_METHODS <= methods


def test_pre_restore_manifest_shape_from_injected_pieces(tmp_path: Path) -> None:
    module = _load_manifest_module()
    claimed = [
        {
            "work_item_id": "wi-1",
            "queue": "execution",
            "task_id": "T-1",
            "repository_id": "aicc",
            "current_attempt_id": "att-1",
        }
    ]
    task_map = {"T-1": "aios-99"}
    leases = [{"task": "T-1", "state": "held", "worktree": "/tmp/wt"}]
    manifest = module.build_manifest(
        claimed_items=claimed,
        task_map=task_map,
        leases=leases,
        lease_error=None,
        captured_at="2026-08-20T00:00:00Z",
    )
    assert manifest["version"] == 1
    assert manifest["captured_at"] == "2026-08-20T00:00:00Z"
    assert manifest["claimed_work"] == [
        {
            "work_item_id": "wi-1",
            "queue": "execution",
            "task_id": "T-1",
            "repository_id": "aicc",
            "current_attempt_id": "att-1",
            "aios_task_id": "aios-99",
            "held_leases": leases,
        }
    ]
    assert manifest["task_map"] == task_map
    assert manifest["leases"] == leases
    assert manifest["lease_list_error"] is None

    out = tmp_path / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(MANIFEST_SCRIPT),
            "--out",
            str(out),
            "--skip-queue",
            "--skip-leases",
            "--task-map",
            str(tmp_path / "missing-map.json"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["claimed_work"] == []
    assert written["leases"] == []
    assert written["task_map"] == {}


def test_list_held_leases_silent_without_dsn() -> None:
    module = _load_manifest_module()
    rows, error = module.list_held_leases(env={})
    assert rows == []
    assert error is None


def test_list_held_leases_reports_refused_authority() -> None:
    module = _load_manifest_module()

    class _Result:
        returncode = 2
        stdout = ""
        stderr = "VOYN_LEASE_REFUSED"

    rows, error = module.list_held_leases(
        env={"VOYN_LEASE_DSN": "host=example dbname=platform"},
        runner=lambda *a, **k: _Result(),
    )
    assert rows == []
    assert error is not None and "refused" in error
