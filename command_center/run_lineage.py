"""Canonical task-to-production provenance over one run.

Persistence lives in :mod:`command_center.runtime.db`; this domain module owns the
single read projection and the evidence-specific write rules. Missing evidence
stays ``None`` and is named in ``unknown_fields`` — readers never infer a deploy,
acceptance, PR, CI result, or repository identity from nearby source state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from command_center.models import iso_now
from command_center.runtime import db


class ImmutableEvidenceError(ValueError):
    """An immutable accepted/deployed fact was already recorded differently."""


class UnverifiedEvidenceError(ValueError):
    """Acceptance/deployment was requested without target verification."""


class DomainEvidenceAdapter:
    """Normalize one domain-native event without discarding its payload."""

    def __init__(self, name: str) -> None:
        self.name = name

    def normalize(self, *, candidate_sha: str | None, payload: dict) -> dict:
        native = dict(payload)
        canonical = json.dumps(native, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        integrity_id = native.get("integrity_id") or native.get("event_id")
        if not integrity_id:
            integrity_id = hashlib.sha256(
                f"{self.name}\0{canonical}".encode("utf-8")
            ).hexdigest()
        return {
            "integrity_id": str(integrity_id),
            "adapter": self.name,
            "status": str(native.get("status") or "observed"),
            "candidate_sha": candidate_sha,
            "reported_sha": native.get("immutable_sha") or native.get("reported_sha"),
            "native_payload": native,
            "observed_at": str(native.get("observed_at") or iso_now()),
        }


class GitHubEvidenceAdapter(DomainEvidenceAdapter):
    def __init__(self) -> None:
        super().__init__("github")

    def normalize(self, *, candidate_sha: str, payload: dict) -> dict:
        event = super().normalize(candidate_sha=candidate_sha, payload=payload)
        reported_sha = payload.get("head_sha")
        checks = _completed_checks(payload.get("checks") or [])
        all_green = bool(checks) and all(
            check.get("conclusion") == "SUCCESS" for check in checks
        )
        expected_count = sum(1 for check in payload.get("checks") or [] if isinstance(check, dict))
        all_completed = len(checks) == expected_count
        if reported_sha != candidate_sha:
            status = "rejected_sha_mismatch"
        elif payload.get("draft") or not payload.get("accepted"):
            status = "unaccepted"
        elif not all_completed or not all_green:
            status = "ci_unaccepted"
        else:
            status = "accepted"
        event.update(
            {
                "status": status,
                "reported_sha": reported_sha,
                "ci": checks,
                "accepted_sha": candidate_sha if status == "accepted" else None,
            }
        )
        return event


class RuntimeProbeAdapter(DomainEvidenceAdapter):
    def __init__(self) -> None:
        super().__init__("runtime_probe")

    def normalize(self, *, candidate_sha: str, payload: dict) -> dict:
        event = super().normalize(candidate_sha=candidate_sha, payload=payload)
        reported_sha = payload.get("immutable_sha")
        if reported_sha and reported_sha != candidate_sha:
            status = "runtime_sha_mismatch"
            deployment_status = "unverified"
        elif reported_sha:
            status = "runtime_sha_match"
            deployment_status = "observed"
        else:
            status = "runtime_observed"
            deployment_status = "unknown"
        event.update(
            {
                "status": status,
                "reported_sha": reported_sha,
                "deployment_status": deployment_status,
                "journey_verified": payload.get("journey") == "passed",
            }
        )
        return event


class DeploymentEvidenceAdapter(DomainEvidenceAdapter):
    _TRUSTED_SOURCES = frozenset({"github_deployment", "signed_target_manifest"})

    def __init__(self) -> None:
        super().__init__("deployment")

    def normalize(self, *, candidate_sha: str, payload: dict) -> dict:
        event = super().normalize(candidate_sha=candidate_sha, payload=payload)
        source = payload.get("source")
        reported_sha = payload.get("sha")
        trusted = source in self._TRUSTED_SOURCES
        if source == "signed_target_manifest":
            trusted = trusted and payload.get("signed") is True
        verified = (
            trusted
            and payload.get("target_verified") is True
            and reported_sha == candidate_sha
            and payload.get("status", "success") == "success"
        )
        event.update(
            {
                "status": "deployed" if verified else "deployment_unverified",
                "reported_sha": reported_sha,
                "deployment_status": "verified" if verified else "unverified",
                "deployed_sha": candidate_sha if verified else None,
            }
        )
        return event


_SCALAR_UNKNOWN_FIELDS = (
    "repository",
    "worktree",
    "branch",
    "base_branch",
    "base_sha",
    "head_sha",
    "accepted_sha",
    "deployed_sha",
    "initiated_by",
    "prompt",
    "model",
    "actions",
    "reproducibility_hash",
)

#: The exact inputs a reproduction attempt must match: the instruction given
#: to the agent (prompt/prompt_version), the model that executed it
#: (provider_id/provider_metadata_json), the code state it started from
#: (base_sha/branch — the "dataset" for a coding agent), the capabilities it
#: was granted, and the literal command that was run. Two runs sharing this
#: hash were given identical reproduction inputs; runs are free to differ in
#: everything the hash omits (timestamps, pids, output) without breaking
#: equality.
_REPRODUCIBILITY_RUN_FIELDS = (
    "prompt",
    "prompt_version",
    "provider_id",
    "provider_metadata_json",
    "command_json",
    "capability_profile",
    "granted_capabilities",
)
_REPRODUCIBILITY_PROVENANCE_FIELDS = ("repository_path", "branch", "base_sha")


def compute_reproducibility_hash(
    run: dict | None, *, provenance_record: dict | None = None
) -> str | None:
    """Hash the exact reproduction inputs for one run's result, or ``None``
    when the run itself is unknown (never guess an identity for a run that
    was not found)."""
    if run is None:
        return None
    inputs = {field: run.get(field) for field in _REPRODUCIBILITY_RUN_FIELDS}
    inputs.update(
        {field: (provenance_record or {}).get(field) for field in _REPRODUCIBILITY_PROVENANCE_FIELDS}
    )
    canonical = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decode_checks(value: str | None) -> list[dict]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


def _decode_json_object(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _decode_command(value: str | None) -> list | None:
    """`run.command_json` is the literal argv the provider was launched
    with (`list[str] | None`) — the "actions performed" this run's result
    came from."""
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, list) else None


def build_view(
    record: dict | None,
    *,
    run: dict | None = None,
    provider_route_record: dict | None = None,
    provider_attempts: list[dict] | None = None,
    evidence: list[dict] | None = None,
) -> dict | None:
    """Return the one public provenance shape used by API and UI.

    ``run`` carries the initiator/prompt/model/actions inputs that live on
    the `run` row itself rather than `run_provenance` — passed in separately
    because a run can exist (and be worth showing "unknown" for) before its
    provenance row is backfilled, and vice versa for legacy rows.
    """
    if record is None:
        return None
    checks = _decode_checks(record.get("ci_conclusions_json"))
    pr = None
    if record.get("pull_request_number") is not None:
        pr = {
            "number": record.get("pull_request_number"),
            "url": record.get("pull_request_url"),
            "head_sha": record.get("pull_request_head_sha"),
        }
    route = None
    if provider_route_record is not None:
        route = {
            "providers": provider_route_record.get("providers"),
            "max_attempts": provider_route_record.get("max_attempts"),
            "selection_reason": provider_route_record.get("selection_reason"),
            "policy_version": provider_route_record.get("policy_version"),
        }
    attempts = provider_attempts or []
    safe_evidence = [
        {
            key: item.get(key)
            for key in (
                "integrity_id",
                "adapter",
                "status",
                "candidate_sha",
                "reported_sha",
                "observed_at",
            )
        }
        for item in (evidence or [])
    ]
    run = run or {}
    initiated_by = run.get("launch_source")
    prompt = run.get("prompt")
    model = run.get("provider_id")
    model_metadata = _decode_json_object(run.get("provider_metadata_json"))
    actions = _decode_command(run.get("command_json"))
    view = {
        "run_id": record.get("run_id"),
        "task_id": record.get("task_id"),
        "repository": record.get("repository_path"),
        "worktree": record.get("worktree_path"),
        "branch": record.get("branch"),
        "base_branch": record.get("base_branch"),
        "base_sha": record.get("base_sha"),
        "head_sha": record.get("head_sha"),
        "pr": pr,
        "ci": checks,
        "ci_observed_at": record.get("ci_observed_at"),
        "accepted_sha": record.get("accepted_sha"),
        "accepted_at": record.get("accepted_at"),
        "deployed_sha": record.get("deployed_sha"),
        "deployment_environment": record.get("deployment_environment"),
        "deployed_at": record.get("deployed_at"),
        "deployment_verified_at": record.get("deployment_verified_at"),
        "provider_route": route,
        "provider_attempts": attempts,
        "evidence": safe_evidence,
        "initiated_by": initiated_by,
        "prompt": prompt,
        "prompt_version": run.get("prompt_version"),
        "model": model,
        "model_metadata": model_metadata,
        "actions": actions,
        "reproducibility_hash": compute_reproducibility_hash(run or None, provenance_record=record),
    }
    unknown = [name for name in _SCALAR_UNKNOWN_FIELDS if view.get(name) is None]
    if pr is None:
        unknown.append("pr")
    if not checks:
        unknown.append("ci")
    if route is None:
        unknown.append("provider_route")
    if not attempts:
        unknown.append("provider_attempts")
    if not safe_evidence:
        unknown.append("evidence")
    if model_metadata is None:
        unknown.append("model_metadata")
    view["unknown_fields"] = unknown
    return view


def get_view(db_path: Path, run_id: str) -> dict | None:
    evidence = db.get_provenance_evidence_for_runs(db_path, [run_id])
    return build_view(
        db.get_run_provenance(db_path, run_id),
        run=db.get_run(db_path, run_id),
        provider_route_record=db.get_provider_route(db_path, run_id),
        provider_attempts=db.list_provider_attempts(db_path, run_id),
        evidence=evidence.get(run_id),
    )


def views_for_runs(db_path: Path, run_ids: Iterable[str]) -> dict[str, dict]:
    ids = list(run_ids)
    records = db.get_run_provenance_for_runs(db_path, ids)
    runs = db.get_runs_for_ids(db_path, ids)
    routes = db.get_provider_routes_for_runs(db_path, ids)
    attempts = db.get_provider_attempts_for_runs(db_path, ids)
    evidence = db.get_provenance_evidence_for_runs(db_path, ids)
    return {
        run_id: build_view(
            record,
            run=runs.get(run_id),
            provider_route_record=routes.get(run_id),
            provider_attempts=attempts.get(run_id),
            evidence=evidence.get(run_id),
        )
        for run_id, record in records.items()
    }


def update_identity(
    db_path: Path,
    run_id: str,
    *,
    repository: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
) -> dict:
    fields = {
        "repository_path": repository,
        "worktree_path": worktree,
        "branch": branch,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    return db.update_run_provenance(
        db_path, run_id, fields={key: value for key, value in fields.items() if value is not None}
    )


def _completed_checks(checks: Iterable[dict]) -> list[dict]:
    completed: list[dict] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "").upper()
        conclusion = check.get("conclusion")
        if status != "COMPLETED" or not conclusion:
            continue
        completed.append(
            {
                "name": check.get("name"),
                "status": status,
                "conclusion": str(conclusion).upper(),
            }
        )
    return sorted(completed, key=lambda item: str(item.get("name") or ""))


def observe_pull_request(
    db_path: Path,
    run_id: str,
    *,
    number: int,
    url: str | None,
    head_sha: str | None,
    checks: Iterable[dict],
    observed_at: str | None = None,
) -> dict:
    completed = _completed_checks(checks)
    return db.update_run_provenance(
        db_path,
        run_id,
        fields={
            "pull_request_number": number,
            "pull_request_url": url,
            "pull_request_head_sha": head_sha,
            "ci_conclusions_json": json.dumps(completed, sort_keys=True),
            "ci_observed_at": observed_at or iso_now(),
        },
    )


def _record_immutable(
    db_path: Path,
    run_id: str,
    *,
    field: str,
    value: str,
    fields: dict,
) -> dict:
    current, matched = db.set_run_provenance_once(
        db_path, run_id, field=field, value=value, fields=fields
    )
    if not matched:
        raise ImmutableEvidenceError(
            f"{field} for run {run_id!r} is immutable: "
            f"{current.get(field)!r} != {value!r}"
        )
    return current


def record_acceptance(
    db_path: Path,
    run_id: str,
    *,
    accepted_sha: str,
    target_verified: bool,
    observed_at: str | None = None,
) -> dict:
    if not target_verified:
        raise UnverifiedEvidenceError("accepted_sha requires target verification")
    observed = observed_at or iso_now()
    return _record_immutable(
        db_path,
        run_id,
        field="accepted_sha",
        value=accepted_sha,
        fields={"accepted_sha": accepted_sha, "accepted_at": observed},
    )


def record_deployment(
    db_path: Path,
    run_id: str,
    *,
    deployed_sha: str,
    environment: str,
    target_verified: bool,
    observed_at: str | None = None,
) -> dict:
    if not target_verified:
        raise UnverifiedEvidenceError("deployed_sha requires target verification")
    current = db.get_run_provenance(db_path, run_id)
    if current is None:
        raise KeyError(f"No provenance for run: {run_id!r}")
    if current.get("deployed_sha") and current["deployed_sha"] != deployed_sha:
        raise ImmutableEvidenceError(
            f"deployed_sha for run {run_id!r} is immutable: "
            f"{current['deployed_sha']!r} != {deployed_sha!r}"
        )
    if not current.get("accepted_sha"):
        raise UnverifiedEvidenceError("deployment requires accepted_sha evidence")
    if current["accepted_sha"] != deployed_sha:
        raise UnverifiedEvidenceError("deployed_sha must equal the accepted_sha for this run")
    observed = observed_at or iso_now()
    return _record_immutable(
        db_path,
        run_id,
        field="deployed_sha",
        value=deployed_sha,
        fields={
            "deployed_sha": deployed_sha,
            "deployment_environment": environment,
            "deployed_at": observed,
            "deployment_verified_at": observed,
        },
    )


def upsert_native_event(db_path: Path, run_id: str, event: dict) -> dict:
    """Persist a normalized event idempotently and round-trip native evidence."""
    native_payload = event.get("native_payload") or {}
    normalized = {key: value for key, value in event.items() if key != "native_payload"}
    observed_at = str(event.get("observed_at") or iso_now())
    row = db.create_provenance_evidence(
        db_path,
        run_id=run_id,
        integrity_id=str(event["integrity_id"]),
        adapter=str(event["adapter"]),
        status=str(event["status"]),
        candidate_sha=event.get("candidate_sha"),
        reported_sha=event.get("reported_sha"),
        native_payload_json=json.dumps(native_payload, ensure_ascii=False, sort_keys=True),
        normalized_json=json.dumps(normalized, ensure_ascii=False, sort_keys=True),
        observed_at=observed_at,
    )
    return {
        **json.loads(row["normalized_json"]),
        "integrity_id": row["integrity_id"],
        "adapter": row["adapter"],
        "status": row["status"],
        "candidate_sha": row["candidate_sha"],
        "reported_sha": row["reported_sha"],
        "native_payload": json.loads(row["native_payload_json"]),
        "observed_at": row["observed_at"],
    }
