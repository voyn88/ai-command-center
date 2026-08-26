"""Build and execute a deterministic, exactly-once pytest shard manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DURATIONS = ROOT / "scripts" / "ci" / "test_durations.json"
MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CollectedTest:
    nodeid: str
    markers: frozenset[str]


class _CollectionPlugin:
    def __init__(self) -> None:
        self.tests: list[CollectedTest] = []

    def pytest_collection_finish(self, session: Any) -> None:
        self.tests = [
            CollectedTest(
                nodeid=item.nodeid,
                markers=frozenset(marker.name for marker in item.iter_markers()),
            )
            for item in session.items
        ]


def collect_tests(pytest_args: Sequence[str] = ()) -> list[CollectedTest]:
    """Collect the canonical suite and reject collection errors fail-closed."""
    import pytest

    plugin = _CollectionPlugin()
    rc = pytest.main(
        ["--collect-only", "-q", "--disable-warnings", *pytest_args],
        plugins=[plugin],
    )
    if rc != pytest.ExitCode.OK:
        raise RuntimeError(f"pytest collection failed with exit code {int(rc)}")
    if not plugin.tests:
        raise RuntimeError("pytest collection produced an empty suite")
    return plugin.tests


def load_duration_history(path: Path) -> tuple[dict[str, float], float, str]:
    """Return valid measured weights; corrupt history falls back to equal weights."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}, 1.0, "missing-or-invalid"
    except json.JSONDecodeError:
        return {}, 1.0, "missing-or-invalid"

    durations = raw.get("durations", {})
    if not isinstance(durations, dict):
        return {}, 1.0, "missing-or-invalid"
    valid = {
        str(nodeid): float(seconds)
        for nodeid, seconds in durations.items()
        if isinstance(seconds, (int, float)) and float(seconds) >= 0
    }
    default = raw.get("default_seconds", 1.0)
    if not isinstance(default, (int, float)) or float(default) <= 0:
        default = 1.0
    source = str(raw.get("source_run", "unknown"))
    return valid, float(default), source


def assign_nodeids(
    nodeids: Sequence[str],
    durations: dict[str, float],
    default_seconds: float,
    shard_count: int,
) -> list[list[str]]:
    """Longest-processing-time assignment with stable path/index tie-breaks."""
    if shard_count < 1:
        raise ValueError("shard_count must be >= 1")
    if default_seconds <= 0:
        raise ValueError("default_seconds must be > 0")
    if len(set(nodeids)) != len(nodeids):
        raise ValueError("canonical nodeids contain duplicates")

    ordered = sorted(
        nodeids,
        key=lambda nodeid: (-durations.get(nodeid, default_seconds), nodeid),
    )
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0.0] * shard_count
    for nodeid in ordered:
        target = min(range(shard_count), key=lambda index: (totals[index], index))
        shards[target].append(nodeid)
        totals[target] += durations.get(nodeid, default_seconds)
    return [sorted(shard) for shard in shards]


def _manifest_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_manifest(
    tests: Sequence[CollectedTest],
    *,
    shard_count: int,
    durations: dict[str, float],
    default_seconds: float,
    duration_source: str,
) -> dict[str, Any]:
    """Partition core/serial/e2e tests and prove the partition is exhaustive."""
    categories: dict[str, list[str]] = {"core": [], "serial": [], "e2e": []}
    for test in tests:
        is_serial = "serial" in test.markers
        is_e2e = "e2e" in test.markers
        if is_serial and is_e2e:
            raise ValueError(f"test cannot be both serial and e2e: {test.nodeid}")
        category = "e2e" if is_e2e else "serial" if is_serial else "core"
        categories[category].append(test.nodeid)

    core_shards = assign_nodeids(
        categories["core"], durations, default_seconds, shard_count
    )
    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "shard_count": shard_count,
        "duration_source": duration_source,
        "partitions": {
            "core": core_shards,
            "serial": sorted(categories["serial"]),
            "e2e": sorted(categories["e2e"]),
        },
    }
    payload["digest"] = _manifest_digest(payload)
    validate_manifest(payload)
    return payload


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Reject empty, overlapping, malformed or tampered manifests."""
    digest = manifest.get("digest")
    unsigned = {key: value for key, value in manifest.items() if key != "digest"}
    if digest != _manifest_digest(unsigned):
        raise ValueError("manifest digest mismatch")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported manifest schema")

    partitions = manifest.get("partitions")
    if not isinstance(partitions, dict) or set(partitions) != {"core", "serial", "e2e"}:
        raise ValueError(
            "manifest must contain exactly core, serial and e2e partitions"
        )
    core = partitions["core"]
    shard_count = manifest.get("shard_count")
    if not isinstance(shard_count, int) or shard_count < 1 or len(core) != shard_count:
        raise ValueError("core shard count does not match manifest")
    if any(not isinstance(shard, list) or not shard for shard in core):
        raise ValueError("every core shard must be non-empty")
    if not isinstance(partitions["serial"], list) or not partitions["serial"]:
        raise ValueError("serial partition must be non-empty")
    if not isinstance(partitions["e2e"], list) or not partitions["e2e"]:
        raise ValueError("e2e partition must be non-empty")

    nodeids = [nodeid for shard in core for nodeid in shard]
    nodeids.extend(partitions["serial"])
    nodeids.extend(partitions["e2e"])
    if not nodeids or any(
        not isinstance(nodeid, str) or not nodeid for nodeid in nodeids
    ):
        raise ValueError("manifest contains an invalid nodeid")
    duplicates = [nodeid for nodeid, count in Counter(nodeids).items() if count != 1]
    if duplicates:
        raise ValueError(f"manifest nodeids are not exactly-once: {duplicates[:5]}")


def _load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    return manifest


def _selected_nodeids(
    manifest: dict[str, Any], partition: str, shard: int | None
) -> list[str]:
    partitions = manifest["partitions"]
    if partition == "core":
        if shard is None or not 1 <= shard <= manifest["shard_count"]:
            raise ValueError("core partition requires a valid 1-based --shard")
        return partitions["core"][shard - 1]
    if shard is not None:
        raise ValueError("--shard is valid only for the core partition")
    return partitions[partition]


def _build_command(args: argparse.Namespace) -> int:
    durations, default_seconds, source = load_duration_history(args.durations)
    manifest = build_manifest(
        collect_tests(args.pytest_arg),
        shard_count=args.shards,
        durations=durations,
        default_seconds=default_seconds,
        duration_source=source,
    )
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    counts = manifest["partitions"]
    print(
        f"manifest={manifest['digest']} core={sum(map(len, counts['core']))} "
        f"serial={len(counts['serial'])} e2e={len(counts['e2e'])}"
    )
    return 0


def _run_command(args: argparse.Namespace) -> int:
    manifest = _load_manifest(args.manifest)
    nodeids = _selected_nodeids(manifest, args.partition, args.shard)
    command = [sys.executable, "-m", "pytest", *args.pytest_arg, *nodeids]
    print(f"running {len(nodeids)} exactly-once tests from {manifest['digest']}")
    return subprocess.run(command, check=False).returncode


def _verify_command(args: argparse.Namespace) -> int:
    manifests = [_load_manifest(path) for path in args.manifests]
    digests = {manifest["digest"] for manifest in manifests}
    if len(digests) != 1:
        raise ValueError(f"shard jobs produced different manifests: {sorted(digests)}")
    print(f"verified {len(manifests)} identical manifests: {digests.pop()}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--shards", type=int, required=True)
    build.add_argument("--durations", type=Path, default=DEFAULT_DURATIONS)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--pytest-arg", action="append", default=[])
    build.set_defaults(handler=_build_command)

    run = subparsers.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--partition", choices=("core", "serial", "e2e"), required=True)
    run.add_argument("--shard", type=int)
    run.add_argument("--pytest-arg", action="append", default=[])
    run.set_defaults(handler=_run_command)

    verify = subparsers.add_parser("verify")
    verify.add_argument("manifests", nargs="+", type=Path)
    verify.set_defaults(handler=_verify_command)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return int(args.handler(args))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"test shard manifest error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
