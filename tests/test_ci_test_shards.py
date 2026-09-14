"""Regression coverage for CI's planned-versus-actually-collected test gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "test_shards.py"
SPEC = importlib.util.spec_from_file_location("ci_test_shards", SCRIPT)
assert SPEC and SPEC.loader
shards = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shards
SPEC.loader.exec_module(shards)


def _manifest() -> dict:
    tests = [
        shards.CollectedTest(f"tests/test_core.py::test_{number}", frozenset())
        for number in range(4)
    ]
    tests.extend(
        [
            shards.CollectedTest("tests/test_serial.py::test_one", frozenset({"serial"})),
            shards.CollectedTest("tests/test_e2e.py::test_one", frozenset({"e2e"})),
        ]
    )
    return shards.build_manifest(
        tests,
        shard_count=4,
        durations={},
        default_seconds=1.0,
        duration_source="test",
    )


def _write_report(path: Path, manifest: dict, collections: list[dict]) -> None:
    report = {
        "schema_version": shards.COLLECTION_REPORT_SCHEMA_VERSION,
        "manifest_digest": manifest["digest"],
        "collections": collections,
    }
    report["digest"] = shards._report_digest(report)
    path.write_text(json.dumps(report), encoding="utf-8")


def _planned_collection(manifest: dict, partition: str, shard: int | None) -> dict:
    return {
        "partition": partition,
        "shard": shard,
        "nodeids": shards._selected_nodeids(manifest, partition, shard),
    }


def test_verify_requires_actual_collections_to_equal_the_prepared_plan(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest_path = tmp_path / "test-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_paths = [tmp_path / f"receipt-{shard}.json" for shard in range(1, 5)]
    _write_report(
        report_paths[0],
        manifest,
        [
            _planned_collection(manifest, "core", 1),
            _planned_collection(manifest, "serial", None),
        ],
    )
    _write_report(
        report_paths[1],
        manifest,
        [
            _planned_collection(manifest, "core", 2),
            _planned_collection(manifest, "e2e", None),
        ],
    )
    _write_report(report_paths[2], manifest, [_planned_collection(manifest, "core", 3)])
    _write_report(report_paths[3], manifest, [_planned_collection(manifest, "core", 4)])

    assert (
        shards._verify_command(
            argparse.Namespace(manifest=manifest_path, collection_reports=report_paths)
        )
        == 0
    )

    wrong = _planned_collection(manifest, "core", 3)
    wrong["nodeids"] = _planned_collection(manifest, "core", 4)["nodeids"]
    _write_report(report_paths[2], manifest, [wrong])

    with pytest.raises(ValueError, match="collected test union"):
        shards._verify_command(
            argparse.Namespace(manifest=manifest_path, collection_reports=report_paths)
        )


def test_run_records_nodeids_collected_by_the_pytest_invocation(tmp_path: Path) -> None:
    previous_directory = Path.cwd()
    os.chdir(tmp_path)
    try:
        test_file = Path("test_live_collection.py")
        test_file.write_text("def test_live():\n    assert True\n", encoding="utf-8")
        nodeid = f"{test_file}::test_live"
        manifest = shards.build_manifest(
            [
                shards.CollectedTest(nodeid, frozenset()),
                shards.CollectedTest(
                    "tests/test_serial.py::test_one", frozenset({"serial"})
                ),
                shards.CollectedTest("tests/test_e2e.py::test_one", frozenset({"e2e"})),
            ],
            shard_count=1,
            durations={},
            default_seconds=1.0,
            duration_source="test",
        )
        manifest_path = Path("test-manifest.json")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        receipt_path = Path("receipt.json")

        assert (
            shards._run_command(
                argparse.Namespace(
                    manifest=manifest_path,
                    partition="core",
                    shard=1,
                    collected_output=receipt_path,
                    pytest_arg=["-q"],
                )
            )
            == 0
        )

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["manifest_digest"] == manifest["digest"]
        assert receipt["collections"] == [
            {"partition": "core", "shard": 1, "nodeids": [nodeid]}
        ]
    finally:
        os.chdir(previous_directory)


def test_run_receipt_uses_the_live_collection_not_the_planned_nodeids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    manifest_path = tmp_path / "test-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt_path = tmp_path / "live-receipt.json"
    actual_nodeid = "tests/test_runtime.py::test_collected_during_run"

    class FakePytest:
        """Two calls now: a collect-only pass that carries the plugin, then
        the real run without it. Under `-n auto` the controller never sees the
        items, so the receipt cannot be taken from the run itself."""

        calls: list[list[str]] = []

        @staticmethod
        def main(command: list[str], *, plugins: list[object] | None = None) -> int:
            FakePytest.calls.append(command)
            if plugins is not None:
                (plugin,) = plugins
                plugin.tests = [shards.CollectedTest(actual_nodeid, frozenset())]
            return 0

    monkeypatch.setitem(sys.modules, "pytest", FakePytest)
    FakePytest.calls = []
    assert (
        shards._run_command(
            argparse.Namespace(
                manifest=manifest_path,
                partition="core",
                shard=1,
                collected_output=receipt_path,
                pytest_arg=["-q"],
            )
        )
        == 0
    )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["collections"][0]["nodeids"] == [actual_nodeid]


def test_the_receipt_is_collected_with_xdist_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure this guards against is silent, not loud.

    Under `-n auto` pytest collects in xdist's workers, so a plugin attached
    to the real run sees no items in the controller and the receipt comes back
    empty — which is how the first live run of build-once failed, after 1304
    tests had passed. The receipt is therefore taken by its own collect-only
    pass with xdist switched off; if that pass ever inherits the run's
    parallel flags again, this fails.
    """
    manifest = _manifest()
    manifest_path = tmp_path / "test-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt = tmp_path / "collection.json"
    seen: list[list[str]] = []

    class FakePytest:
        @staticmethod
        def main(command: list[str], *, plugins: list[object] | None = None) -> int:
            seen.append(command)
            if plugins is not None:
                (plugin,) = plugins
                plugin.tests = [shards.CollectedTest("tests/test_a.py::test_one", frozenset())]
            return 0

    monkeypatch.setitem(sys.modules, "pytest", FakePytest)
    shards._run_command(
        argparse.Namespace(
            manifest=manifest_path,
            partition="core",
            shard=1,
            collected_output=receipt,
            pytest_arg=["-n", "auto", "--dist", "loadscope"],
        )
    )

    collect_pass, run_pass = seen
    assert "--collect-only" in collect_pass
    assert collect_pass[collect_pass.index("-p") + 1] == "no:xdist"
    assert "-n" not in collect_pass
    # The real run keeps its parallelism; only the receipt pass gives it up.
    assert "-n" in run_pass
