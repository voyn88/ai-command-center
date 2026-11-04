"""Measure a configured release target using the separate operator authority.

This is called by the existing release owner, never by a role job or owner API.
The receipt proves deployment observation, not business criterion acceptance.
Target configuration and the artifact directory must be operator-controlled.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class ReleaseTarget:
    repo: str
    version_url: str
    health_url: str
    sha_field: str = "sha"
    health_field: str = "status"
    healthy_value: str = "ok"

    def validate(self) -> None:
        urls = [urlsplit(self.version_url), urlsplit(self.health_url)]
        if not self.repo or any(
            u.scheme != "https" or not u.hostname or u.username or u.password
            or u.query or u.fragment for u in urls
        ) or urls[0].netloc != urls[1].netloc:
            raise ValueError("release target requires same-origin HTTPS endpoints")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("release measurement must not follow redirects")


def _https_url(url: str):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
        _ = parsed.port
    except (TypeError, ValueError):
        raise ValueError("observation URL requires HTTPS without credentials or query") from None
    return parsed


def _auth_headers(*, bearer=None, basic=None):
    if bearer is not None and basic is not None:
        raise ValueError("conflicting observation credentials")
    if basic is not None:
        if (not isinstance(basic, tuple) or len(basic) != 2
                or not all(isinstance(v, str) and v for v in basic)
                or ":" in basic[0]
                or any(ord(c) < 32 or ord(c) == 127 for v in basic for c in v)):
            raise ValueError("invalid observation credential")
        try:
            encoded = base64.b64encode(":".join(basic).encode("utf-8")).decode("ascii")
        except UnicodeError:
            raise ValueError("invalid observation credential") from None
        return {"Authorization": "Basic " + encoded}
    if bearer is not None:
        if not isinstance(bearer, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", bearer):
            raise ValueError("invalid observation credential")
        return {"Authorization": "Bearer " + bearer}
    return {}


def fetch_artifact_bytes(url: str, *, bearer: str | None = None,
                         basic: tuple[str, str] | None = None) -> bytes:
    """Bounded private read; errors never echo endpoint, response or credentials."""
    _https_url(url)
    headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
    headers.update(_auth_headers(bearer=bearer, basic=basic))
    try:
        request = Request(url, headers=headers)
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError
    except Exception:
        raise ValueError("observation fetch failed") from None
    return raw


def fetch_observation(url: str, *, bearer: str | None = None,
                      basic: tuple[str, str] | None = None) -> dict:
    auth = {"basic": basic} if basic is not None else {"bearer": bearer}
    _auth_headers(bearer=bearer, basic=basic)
    raw = fetch_artifact_bytes(url, **auth)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("invalid observation JSON") from None
    if not isinstance(value, dict):
        raise ValueError("release endpoint must return an object")
    return value


def configured_observer(origin: str, *, bearer: str | None = None,
                        basic: tuple[str, str] | None = None):
    """Private credential stays in this callable, pinned to one operator origin."""
    allowed = _https_url(origin).netloc
    _auth_headers(bearer=bearer, basic=basic)
    auth = {"basic": basic} if basic is not None else {"bearer": bearer}

    def observe(url: str) -> dict:
        if _https_url(url).netloc != allowed:
            raise ValueError("foreign observation origin")
        return fetch_observation(url, **auth)

    return observe


def configured_artifact_reader(origin: str, *, bearer: str | None = None,
                               basic: tuple[str, str] | None = None):
    """Byte-preserving variant for hashing retained check artifacts."""
    allowed = _https_url(origin).netloc
    _auth_headers(bearer=bearer, basic=basic)
    auth = {"basic": basic} if basic is not None else {"bearer": bearer}

    def read(url: str) -> bytes:
        if _https_url(url).netloc != allowed:
            raise ValueError("foreign observation origin")
        return fetch_artifact_bytes(url, **auth)

    return read


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate acceptance artifact field")
        result[key] = value
    return result


def _verified_check_artifact(artifact_url, sha256, expected, read, kind):
    """Read once and verify bytes and all expected bindings before admission."""
    try:
        raw = read(artifact_url)
    except Exception:
        raise ValueError(f"{kind} artifact fetch failed") from None
    if (not isinstance(raw, bytes) or len(raw) > 65536
            or hashlib.sha256(raw).hexdigest() != sha256):
        raise ValueError(f"{kind} artifact hash or size mismatch")
    try:
        artifact = json.loads(raw, object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeError):
        raise ValueError(f"invalid {kind} artifact JSON") from None
    if (not isinstance(artifact, dict) or set(artifact) != set(expected) | {"observed_at"}
            or any(type(artifact.get(k)) is not int for k, v in expected.items() if type(v) is int)
            or any(artifact.get(k) != v for k, v in expected.items())):
        raise ValueError(f"{kind} artifact does not match authoritative scope")
    try:
        observed_at = datetime.fromisoformat(artifact["observed_at"].replace("Z", "+00:00"))
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{kind} artifact requires an aware timestamp") from None
    return artifact_url + "#sha256=" + sha256, observed_at


def record_acceptance_artifact(
    connection, *, task_id: str, revision: int, repo: str, criterion_id: str,
    check_id: str, artifact_url: str, sha256: str, read=fetch_artifact_bytes,
) -> str:
    """Operator-only admission of an existing, independently produced check.

    Caller selects a trusted check producer and expected check ID, never a model
    supplied URL or checksum. This function does not execute or author checks.
    SQL independently binds admission to the current immutable release receipt
    and enforces timestamp freshness. Caller owns commit/rollback. A retained
    artifact must remain readable by authorized reviewers on other hosts.
    """
    _https_url(artifact_url)
    if (type(revision) is not int or revision < 1
            or not all(isinstance(v, str) and v.strip() for v in
                       (task_id, repo, criterion_id, check_id))
            or not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256)):
        raise ValueError("invalid acceptance observation scope")
    with connection.cursor() as cur:
        cur.execute("SELECT delivery_team_release_scope(%s,%s)", (task_id, revision))
        row = cur.fetchone()
        scope = row[0] if row else None
    if (not isinstance(scope, dict) or scope.get("repo") != repo
            or criterion_id not in scope.get("criteria", [])
            or not isinstance(scope.get("source_sha"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", scope["source_sha"])):
        raise ValueError("missing or foreign acceptance scope")
    expected = {
        "schema": "delivery-acceptance-observation/v1", "task_id": task_id,
        "revision": revision, "sha": scope["source_sha"], "environment": "production",
        "criterion_id": criterion_id, "check_id": check_id, "outcome": "passed",
    }
    reference, observed_at = _verified_check_artifact(
        artifact_url, sha256, expected, read, "acceptance")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT delivery_team_record_acceptance(%s,%s,%s,%s,%s,%s,%s)",
            (task_id, revision, scope["source_sha"], criterion_id, check_id,
             reference, observed_at),
        )
        result = cur.fetchone()
        if not result or result[0] is not True:
            raise ValueError("acceptance observation refused")
    return reference


def record_corrective_artifact(
    connection, *, parent_task_id: str, revision: int, incident_key: str,
    corrective_task_id: str, corrective_revision: int, sha: str, criterion_id: str, check_id: str,
    artifact_url: str, sha256: str, read=fetch_artifact_bytes,
) -> str:
    """Admit a retained corrective-effectiveness observation as the operator.

    All expected fields and the producer URL/hash come from the trusted operator
    plan, never an auditor/CTO model output. SQL must recheck the current parent
    revision, incident/action assignment, its effectiveness criterion, completed
    correction SHA and observation freshness. This adapter neither authors proof
    nor asserts that an arbitrary green CI run establishes effectiveness.
    """
    _https_url(artifact_url)
    if (type(revision) is not int or revision < 1
            or type(corrective_revision) is not int or corrective_revision < 1
            or not all(isinstance(v, str) and v.strip() for v in
                       (parent_task_id, corrective_task_id, criterion_id, check_id))
            or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha)
            or not isinstance(incident_key, str) or not re.fullmatch(r"[a-f0-9]{64}", incident_key)
            or not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256)):
        raise ValueError("invalid corrective observation scope")
    expected = {
        "schema": "delivery-corrective-observation/v1", "parent_task_id": parent_task_id,
        "revision": revision, "incident_key": incident_key,
        "corrective_task_id": corrective_task_id, "corrective_revision": corrective_revision,
        "sha": sha,
        "criterion_id": criterion_id, "check_id": check_id, "outcome": "passed",
    }
    reference, observed_at = _verified_check_artifact(
        artifact_url, sha256, expected, read, "corrective")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT delivery_team_record_corrective_check(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (parent_task_id, revision, incident_key, corrective_task_id, corrective_revision,
             sha, criterion_id, check_id, reference, observed_at),
        )
        result = cur.fetchone()
        if not result or result[0] is not True:
            raise ValueError("corrective observation refused")
    return reference


def collect_release_receipt(
    connection, *, task_id: str, revision: int, target: ReleaseTarget,
    artifact_directory: Path, artifact_base_url: str, observe=fetch_observation,
    read_artifact=fetch_artifact_bytes,
) -> Path:
    """Fail closed on stale task, mismatched release or unavailable health.

    Caller owns the existing deployment transaction and commits only on success.
    Operator SQL scope supplies SHA and criterion IDs. Endpoint responses cannot
    choose task, repository, revision or criteria. Raw response fields are never
    persisted, since version endpoints may expose unrelated configuration.
    The existing authenticated artifact endpoint must serve artifact_directory.
    A byte-for-byte readback proves the retained observation is available before
    its HTTPS reference is recorded. This function never uploads or starts a
    server. A failed readback leaves only a local observation, not a receipt.
    """
    target.validate()
    _https_url(artifact_base_url)
    if not artifact_directory.is_absolute():
        raise ValueError("observation artifact directory must be absolute")
    with connection.cursor() as cur:
        cur.execute("SELECT delivery_team_release_scope(%s,%s)", (task_id, revision))
        row = cur.fetchone()
        scope = row[0] if row else None
    if not scope or scope.get("repo") != target.repo:
        raise ValueError("missing or foreign release scope")
    started_at = datetime.now(UTC).isoformat()
    before = observe(target.version_url).get(target.sha_field)
    health = observe(target.health_url).get(target.health_field)
    after = observe(target.version_url).get(target.sha_field)
    if before != scope["source_sha"] or after != before or health != target.healthy_value:
        raise ValueError("release SHA or health observation did not match")
    record = {
        "schema": "delivery-release-observation/v1", "task_id": task_id,
        "revision": revision, "repo": target.repo, "environment": "production",
        "sha": after, "version_url": target.version_url, "health_url": target.health_url,
        "health": health, "criteria": scope["criteria"], "started_at": started_at,
        "observed_at": datetime.now(UTC).isoformat(),
        "business_acceptance": False,
    }
    raw = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    artifact_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact = artifact_directory / (digest + ".json")
    with artifact.open("xb") as stream:
        stream.write(raw)
    artifact.chmod(0o600)
    artifact_url = artifact_base_url.rstrip("/") + "/" + artifact.name
    try:
        retained = read_artifact(artifact_url)
    except Exception:
        raise ValueError("release artifact readback failed") from None
    if not isinstance(retained, bytes) or retained != raw:
        raise ValueError("release artifact readback mismatch")
    # SQL rechecks revision, current BA evidence and SHA after the observations.
    with connection.cursor() as cur:
        cur.execute(
            "SELECT delivery_team_record_release(%s,%s,%s,'production',%s::jsonb,%s)",
            (task_id, revision, after, json.dumps(scope["criteria"]),
             f"{artifact_url}#sha256={digest}"),
        )
        result = cur.fetchone()
        if not result or result[0] is not True:
            raise ValueError("release scope changed during observation")
    return artifact
