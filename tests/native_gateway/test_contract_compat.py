"""Schema-compatibility gates.

1. The committed contract artifacts must match what the code generates —
   silent contract drift fails here.
2. The DTO must remain decodable by the shipped Swift client: required keys
   present, required enum vocabularies unchanged.
"""

from __future__ import annotations

import json

from native_gateway.contract_export import (
    CONTRACT_DIR,
    build_openapi,
    build_snapshot_schema,
)
from native_gateway.dto import EvidenceState, Freshness


def test_committed_openapi_matches_generated():
    committed = json.loads((CONTRACT_DIR / "openapi.json").read_text(encoding="utf-8"))
    assert committed == build_openapi(), (
        "docs/aicc_native_gateway/openapi.json is stale; "
        "run python -m native_gateway.contract_export and review the diff"
    )


def test_committed_snapshot_schema_matches_generated():
    committed = json.loads(
        (CONTRACT_DIR / "schemas/snapshot-1.0.schema.json").read_text(encoding="utf-8")
    )
    assert committed == build_snapshot_schema(), (
        "snapshot-1.0.schema.json is stale; "
        "run python -m native_gateway.contract_export and review the diff"
    )


def test_snapshot_schema_keeps_client_required_surface():
    schema = build_snapshot_schema()
    required = set(schema["required"])
    assert {
        "schemaVersion",
        "revision",
        "generatedAt",
        "freshness",
        "tasks",
        "lanes",
        "events",
    } <= required

    defs = schema["$defs"]
    assert (
        set(defs["Freshness"]["enum"])
        == {f.value for f in Freshness}
        == {
            "fresh",
            "stale",
            "offline",
            "degraded",
        }
    )
    assert set(defs["EvidenceState"]["enum"]) == {e.value for e in EvidenceState}
    evidence_props = set(defs["DeliveryEvidence"]["properties"])
    assert evidence_props == {
        "headSHA",
        "pullRequest",
        "ci",
        "acceptance",
        "mergedSHA",
        "deployedSHA",
    }


def test_openapi_exposes_only_read_routes():
    openapi = build_openapi()
    for path, item in openapi["paths"].items():
        assert path.startswith("/v1/"), path
        assert set(item) <= {"get"}, f"non-GET method exposed on {path}"
