from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from scripts.ci import test_shards


def _tests() -> list[test_shards.CollectedTest]:
    return [
        test_shards.CollectedTest(f"tests/test_core.py::test_{index}", frozenset())
        for index in range(12)
    ] + [
        test_shards.CollectedTest(
            "tests/test_serial.py::test_serial", frozenset({"serial"})
        ),
        test_shards.CollectedTest("tests/test_e2e.py::test_e2e", frozenset({"e2e"})),
    ]


def _manifest(tests: list[test_shards.CollectedTest] | None = None) -> dict:
    return test_shards.build_manifest(
        tests or _tests(),
        shard_count=4,
        durations={"tests/test_core.py::test_0": 20.0},
        default_seconds=1.0,
        duration_source="unit-test",
    )


def test_manifest_is_deterministic_exhaustive_and_exactly_once() -> None:
    first = _manifest()
    second = _manifest(list(reversed(_tests())))

    assert first == second
    partitions = first["partitions"]
    assigned = [nodeid for shard in partitions["core"] for nodeid in shard]
    assigned += partitions["serial"] + partitions["e2e"]
    assert Counter(assigned) == Counter(test.nodeid for test in _tests())
    assert all(partitions["core"])
    assert partitions["serial"]
    assert partitions["e2e"]


def test_duration_balancing_uses_lpt_and_stable_tie_breaks() -> None:
    nodeids = [f"test_{index}" for index in range(8)]
    durations = {"test_0": 8.0, "test_1": 7.0, "test_2": 6.0, "test_3": 5.0}

    first = test_shards.assign_nodeids(nodeids, durations, 1.0, 2)
    second = test_shards.assign_nodeids(list(reversed(nodeids)), durations, 1.0, 2)

    assert first == second
    totals = [sum(durations.get(nodeid, 1.0) for nodeid in shard) for shard in first]
    assert max(totals) - min(totals) <= 1.0


def test_missing_or_corrupt_duration_history_falls_back_without_dropping_tests(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{broken", encoding="utf-8")

    assert test_shards.load_duration_history(missing) == ({}, 1.0, "missing-or-invalid")
    assert test_shards.load_duration_history(corrupt) == ({}, 1.0, "missing-or-invalid")
    shards = test_shards.assign_nodeids(["a", "b"], {}, 1.0, 2)
    assert sorted(nodeid for shard in shards for nodeid in shard) == ["a", "b"]


def test_serial_and_e2e_partitions_are_disjoint() -> None:
    ambiguous = _tests() + [
        test_shards.CollectedTest(
            "tests/test_bad.py::test_bad", frozenset({"serial", "e2e"})
        )
    ]

    with pytest.raises(ValueError, match="both serial and e2e"):
        _manifest(ambiguous)


@pytest.mark.parametrize("partition", ["core", "serial", "e2e"])
def test_empty_required_partition_is_rejected(partition: str) -> None:
    manifest = _manifest()
    unsigned = {key: value for key, value in manifest.items() if key != "digest"}
    if partition == "core":
        unsigned["partitions"]["core"][0] = []
    else:
        unsigned["partitions"][partition] = []
    unsigned["digest"] = test_shards._manifest_digest(unsigned)

    with pytest.raises(ValueError, match="non-empty"):
        test_shards.validate_manifest(unsigned)


def test_duplicate_or_tampered_manifest_is_rejected() -> None:
    duplicate = _manifest()
    unsigned = {
        key: copy.deepcopy(value) for key, value in duplicate.items() if key != "digest"
    }
    unsigned["partitions"]["serial"].append(unsigned["partitions"]["core"][0][0])
    unsigned["digest"] = test_shards._manifest_digest(unsigned)
    with pytest.raises(ValueError, match="not exactly-once"):
        test_shards.validate_manifest(unsigned)

    tampered = _manifest()
    tampered["duration_source"] = "changed"
    with pytest.raises(ValueError, match="digest mismatch"):
        test_shards.validate_manifest(tampered)


def test_verify_rejects_a_receipt_bound_to_a_different_manifest(tmp_path: Path) -> None:
    """The guarantee moved but did not weaken. Four shards no longer each
    rebuild the manifest -- the plan is built once -- so identity between
    copies proves nothing. What must hold instead is that every collection
    receipt was produced against *this* plan; a receipt carrying another
    manifest's digest is a shard that ran something else.
    """
    manifest = _manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    foreign = _manifest()
    foreign["duration_source"] = "different"
    unsigned = {key: value for key, value in foreign.items() if key != "digest"}
    foreign["digest"] = test_shards._manifest_digest(unsigned)

    report = {
        "schema_version": test_shards.COLLECTION_REPORT_SCHEMA_VERSION,
        "manifest_digest": foreign["digest"],
        "collections": [
            {"partition": "core", "shard": 1, "nodeids": ["tests/test_a.py::test_one"]}
        ],
    }
    unsigned_report = {k: v for k, v in report.items() if k != "digest"}
    report["digest"] = test_shards._report_digest(unsigned_report)
    report_path = tmp_path / "collection.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    args = type(
        "Args",
        (),
        {"manifest": manifest_path, "collection_reports": [report_path]},
    )()
    with pytest.raises(ValueError, match="binds a different manifest"):
        test_shards._verify_command(args)