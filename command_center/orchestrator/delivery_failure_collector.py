"""Bounded failure observations for the EXISTING control-plane/release owner.

Call for one known workflow attempt or one retained self-deploy JSONL record.
No polling daemon, rerun, deployment, credentials, task creation or model-input
entrypoint is introduced here. CI uses the existing fleet GitHub quota/auth
boundary; callers must invoke it inside their existing gh_access.tick. Deploy
source configuration and byte offset/hash come from the trusted release owner,
never a role result. Its artifact URI identifies a retained source mapping;
the owner must keep that mapping and the record available across log rotation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from command_center.orchestrator import gh_access, planner, review_merge
from command_center.orchestrator.delivery_incidents import record_incident
from command_center.orchestrator.delivery_team import enabled, incident_key

MAX_RECORD_BYTES = 65536


@dataclass(frozen=True)
class CollectionResult:
    outcome: str  # admitted | ignored | refused; none means accepted delivery
    reason: str
    key: str | None = None


@dataclass(frozen=True)
class DeploymentSource:
    """Operator-controlled mapping to a protected, retained host journal.

    The self-deploy row has no repo/host identity. These fields are trusted
    configuration, not claims authenticated by its JSON or checksum.
    """
    repo: str
    host_id: str
    source_id: str
    path: Path


def _sha(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{40}", value))


def _timestamp(value) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except ValueError:
        return False


def _valid_request(task_id, revision, sha) -> bool:
    return (isinstance(task_id, str)
            and bool(re.fullmatch(r"VOYN-[A-Za-z0-9][A-Za-z0-9._-]*", task_id))
            and type(revision) is int and revision > 0 and _sha(sha))


def _scope(connection, task_id, revision, sha):
    # Historical failures remain facts when a later SHA is published. Require
    # a real canonical evidence binding, not just any failed run in the repo.
    with connection.cursor() as cur:
        cur.execute(
            "SELECT b.repo FROM backlog_task b WHERE b.task_id=%s AND b.revision=%s "
            "AND EXISTS(SELECT 1 FROM delivery_team_stage s WHERE s.task_id=b.task_id) "
            "AND EXISTS(SELECT 1 FROM backlog_evidence e WHERE e.task_id=b.task_id "
            "AND e.kind='sha' AND e.value=%s)", (task_id, revision, sha),
        )
        row = cur.fetchone()
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def _persist(connection, *, task_id, revision, repo, sha, run_ref, signature,
             kind, reference, observed_at, detail):
    # Explicit transaction also works with the existing autocommit pool. SQL
    # admission rechecks current revision/repo. Under a caller transaction this
    # is a nested savepoint: caller owns final commit/rollback and must inspect
    # refusal. An admitted result is not proof of that outer commit.
    with connection.transaction():
        if _scope(connection, task_id, revision, sha) != repo:
            return CollectionResult("refused", "scope_changed")
        accepted = record_incident(
            connection, task_id=task_id, revision=revision, repo=repo, sha=sha,
            run_ref=run_ref, signature=signature, kind=kind, reference=reference,
            observed_at=observed_at, detail=detail,
        )
        if not accepted:
            return CollectionResult("refused", "database_refused")
    return CollectionResult("admitted", kind, incident_key(repo, run_ref, sha, signature))


def collect_ci_failure(connection, *, task_id: str, revision: int,
                       expected_sha: str, run_id: int, attempt: int) -> CollectionResult:
    """Read one exact Actions attempt; an API error is never a CI failure."""
    if not enabled():
        return CollectionResult("ignored", "disabled")
    if (not _valid_request(task_id, revision, expected_sha)
            or type(run_id) is not int or run_id < 1
            or type(attempt) is not int or attempt < 1):
        return CollectionResult("refused", "invalid_request")
    repo = _scope(connection, task_id, revision, expected_sha)
    if repo is None:
        return CollectionResult("refused", "unbound_task_or_sha")
    route = planner.repo_route(repo)
    origin = review_merge._origin_owner_repo(route[1]) if route else None
    if (not origin or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", v) or v in {".", ".."}
                          for v in origin)):
        return CollectionResult("refused", "unrouted_repository")
    quota = gh_access.current_quota()
    if (quota is None or quota.identity != gh_access.FLEET
            or quota.allow_ambient_fallback or quota.degraded):
        return CollectionResult("refused", "fleet_tick_required")
    slug = "/".join(origin).casefold()
    endpoint = f"repos/{slug}/actions/runs/{run_id}/attempts/{attempt}"
    status, run = review_merge._rest_json(route[1], endpoint)
    if status != "ok" or not isinstance(run, dict):
        return CollectionResult("refused", "ci_observation_unavailable")
    repository = run.get("repository")
    if (type(run.get("id")) is not int or run["id"] != run_id
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != attempt
            or not isinstance(repository, dict)
            or not isinstance(repository.get("full_name"), str)
            or repository["full_name"].casefold() != slug
            or run.get("head_sha") != expected_sha):
        return CollectionResult("refused", "foreign_ci_observation")
    if run.get("status") != "completed":
        return CollectionResult("ignored", "ci_not_completed")
    conclusion = run.get("conclusion")
    if not isinstance(conclusion, str):
        return CollectionResult("refused", "invalid_ci_observation")
    if conclusion in {"success", "cancelled", "skipped", "neutral", "action_required"}:
        return CollectionResult("ignored", "ci_non_failure")
    if conclusion not in {"failure", "timed_out"} or not _timestamp(run.get("updated_at")):
        return CollectionResult("refused", "invalid_ci_observation")
    run_ref = f"github-actions:{slug}:{run_id}:{attempt}"
    return _persist(
        connection, task_id=task_id, revision=revision, repo=repo, sha=expected_sha,
        run_ref=run_ref, signature=f"workflow_attempt:{conclusion}", kind="ci_failure",
        reference=f"https://github.com/{slug}/actions/runs/{run_id}/attempts/{attempt}",
        observed_at=run["updated_at"],
        detail=f"GitHub Actions run {run_id} attempt {attempt} completed with {conclusion}.",
    )


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _read_record(source, offset, digest):
    if (not isinstance(source, DeploymentSource) or not isinstance(source.path, Path)
            or not source.path.is_absolute() or not source.repo
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", v)
                   for v in (source.host_id, source.source_id))
            or type(offset) is not int or offset < 0
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not hasattr(os, "O_NOFOLLOW")):
        return None
    try:
        descriptor = os.open(source.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            if offset:
                stream.seek(offset - 1)
                if stream.read(1) != b"\n":
                    return None
            stream.seek(offset)
            raw = stream.readline(MAX_RECORD_BYTES + 1)
        if (not raw.endswith(b"\n") or len(raw) > MAX_RECORD_BYTES
                or hashlib.sha256(raw).hexdigest() != digest):
            return None
        value = json.loads(raw, object_pairs_hook=_unique_fields)
        if not isinstance(value, dict) or set(value) != {"at", "outcome", "detail", "previous_sha", "target_sha"}:
            return None
        if not _timestamp(value["at"]) or not isinstance(value["detail"], str):
            return None
        return value
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def collect_deploy_failure(connection, *, task_id: str, revision: int,
                           expected_sha: str, source: DeploymentSource,
                           offset: int, sha256: str) -> CollectionResult:
    """Read an exact retained row; never copy raw stderr/detail to DB or UI.

    A missing, rotated, partially written or mismatched row is unavailable
    evidence, not a new deployment failure. Successful rollback remains a
    deployment failure fact, not a statement that production is unhealthy.
    """
    if not enabled():
        return CollectionResult("ignored", "disabled")
    if not _valid_request(task_id, revision, expected_sha):
        return CollectionResult("refused", "invalid_request")
    repo = _scope(connection, task_id, revision, expected_sha)
    if repo is None or not isinstance(source, DeploymentSource) or source.repo != repo:
        return CollectionResult("refused", "unbound_deployment_source")
    row = _read_record(source, offset, sha256)
    if row is None:
        return CollectionResult("refused", "deployment_observation_unavailable")
    if row["target_sha"] != expected_sha or not _sha(row["previous_sha"]):
        return CollectionResult("refused", "foreign_deployment_observation")
    outcome = row["outcome"]
    if not isinstance(outcome, str):
        return CollectionResult("refused", "invalid_deployment_observation")
    if outcome in {"noop", "deployed", "refused"}:
        return CollectionResult("ignored", "deployment_non_failure")
    if outcome not in {"failed", "rolled_back"}:
        return CollectionResult("refused", "invalid_deployment_observation")
    reference = f"artifact://{source.host_id}/{source.source_id}/offset/{offset}#sha256={sha256}"
    return _persist(
        connection, task_id=task_id, revision=revision, repo=repo, sha=expected_sha,
        run_ref=f"self-deploy:{source.host_id}:{source.source_id}:{row['at']}:{sha256}",
        signature=f"self_deploy:{outcome}", kind="deployment_failure", reference=reference,
        observed_at=row["at"], detail=f"Self-deploy outcome {outcome} for target {expected_sha}; protected source record SHA256 {sha256}.",
    )
