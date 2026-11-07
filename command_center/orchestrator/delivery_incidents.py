"""Incident observation adapter for existing control-plane hooks.

The caller supplies facts from its completed CI/review/merge/deploy operation.
This module does not fetch evidence, infer failure from a skipped tick, spawn
work, retry, commit a transaction, or assert corrective effectiveness. The
existing hook owns its connection and transaction; SQL fences revision/repo
and idempotency. Never invoke this from model output ingestion.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from command_center.orchestrator.delivery_team import TeamResultError, incident_key

INCIDENT_KINDS = frozenset({
    "ci_failure", "review_rejection", "merge_failure", "deployment_failure",
})


def _text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise TeamResultError(f"invalid incident {field}")
    return value


def record_incident(
    connection: Any,
    *,
    task_id: str,
    revision: int,
    repo: str,
    run_ref: str,
    sha: str,
    signature: str,
    kind: str,
    reference: str,
    observed_at: str,
    detail: str,
) -> bool:
    """Record one actual, referenced failure, returning the SQL admission result.

    False is a refusal (e.g. stale revision), never proof of persistence. DB
    errors propagate to the hook, so an absent observation cannot be reported
    as a successful audit. ``reference`` identifies retained provider output
    or a journal/artifact record; it is not a model's ``verified`` assertion.
    """
    _text(task_id, 512, "task_id")
    if not re.fullmatch(r"VOYN-[A-Za-z0-9][A-Za-z0-9._-]*", task_id):
        raise TeamResultError("invalid incident task_id")
    if type(revision) is not int or revision < 1:
        raise TeamResultError("invalid incident revision")
    _text(repo, 1024, "repo")
    _text(run_ref, 2048, "run_ref")
    _text(signature, 1024, "signature")
    if not isinstance(kind, str) or kind not in INCIDENT_KINDS:
        raise TeamResultError("invalid incident kind")
    _text(reference, 2048, "reference")
    try:
        parsed = urlsplit(reference)
        valid_reference = (
            parsed.scheme in {"https", "journal", "artifact"}
            and bool(parsed.netloc)
            and not parsed.username
            and not parsed.password
        )
    except ValueError as exc:
        raise TeamResultError("invalid incident reference") from exc
    if not valid_reference:
        raise TeamResultError("incident reference must identify retained external evidence")
    _text(observed_at, 64, "observed_at")
    try:
        when = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TeamResultError("invalid incident observed_at") from exc
    if when.tzinfo is None or when.utcoffset() is None:
        raise TeamResultError("incident timestamp requires timezone")
    _text(detail, 4000, "detail")
    key = incident_key(repo, run_ref, sha, signature)
    evidence = {
        "kind": kind,
        "reference": reference,
        "observed_at": observed_at,
        "detail": detail,
    }
    with connection.cursor() as cur:
        cur.execute(
            "SELECT delivery_team_record_incident(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (task_id, revision, key, repo, run_ref, sha, signature, json.dumps(evidence)),
        )
        row = cur.fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not bool:
        raise TeamResultError("invalid incident admission response")
    return row[0]
