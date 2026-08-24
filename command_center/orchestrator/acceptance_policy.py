"""Strict versioned identity and check policy for exact-SHA acceptance."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_KEYS = {
    "schema_version",
    "policy_version",
    "trusted_reviewer_logins",
    "ci_required_check_names",
    "merge_required_check_names",
}
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "config"
    / "aicc-acceptance-policy.json"
)


@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    version: str
    trusted_reviewer_logins: frozenset[str]
    ci_required_check_names: frozenset[str]
    merge_required_check_names: frozenset[str]


def _names(value: Any, field: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} contains an invalid value")
    names = [
        item.casefold() if field == "trusted_reviewer_logins" else item
        for item in value
    ]
    if len(set(names)) != len(names):
        raise ValueError(f"{field} contains duplicates")
    return frozenset(names)


def load(path: str | Path = DEFAULT_POLICY_PATH) -> AcceptancePolicy:
    try:
        raw = json.loads(Path(path).read_bytes())
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("acceptance policy is unreadable") from exc
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise ValueError("acceptance policy has unknown or missing fields")
    if raw["schema_version"] != 1:
        raise ValueError("unsupported acceptance policy schema")
    version = raw["policy_version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("acceptance policy version is invalid")
    ci_required = _names(raw["ci_required_check_names"], "ci_required_check_names")
    merge_required = _names(
        raw["merge_required_check_names"], "merge_required_check_names"
    )
    if not ci_required <= merge_required:
        raise ValueError("merge checks must include every pre-acceptance CI check")
    return AcceptancePolicy(
        version=version,
        trusted_reviewer_logins=_names(
            raw["trusted_reviewer_logins"], "trusted_reviewer_logins"
        ),
        ci_required_check_names=ci_required,
        merge_required_check_names=merge_required,
    )


if __name__ == "__main__":
    load(sys.argv[1] if len(sys.argv) == 2 else DEFAULT_POLICY_PATH)
