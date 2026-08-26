"""Fetch and verify an exact accepted AIOS wheel from a permanent GitHub
Release asset (no mutable fallback).

Two distributions are consumed this way and both go through this one script,
selected by ``--lock``: ``aios-sdk`` (the HTTP client, ``aios-sdk.lock.json``)
and ``aios-db`` (the universal PostgreSQL primitives, ``aios-db.lock.json``).
The verification is identical for both — the same release-identity, tag-binding
and checksum proofs — so it is written once rather than copied and left to
drift.

Earlier revisions pinned a CI Actions artifact; those expire (the pinned
artifact 9042593332 did), turning the fail-closed gate into a permanent
failure. Published GitHub Release assets of an immutable release do not
expire, so the lock now pins a release tag and the exact wheel checksum,
and the fetch additionally proves the tag still points at the accepted
main commit (peeled through the annotated tag object).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "aios-sdk.lock.json"
DB_LOCK_PATH = REPO_ROOT / "aios-db.lock.json"
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
CHECKSUM_MANIFEST_NAME = "SHA256SUMS"
READONLY_TOKEN_ENV = "AIOS_ARTIFACT_READONLY_TOKEN"
LEGACY_TOKEN_ENV = "AIOS_ARTIFACT_READ_TOKEN"

_RELEASE_TAG_PATTERN = re.compile(r"^v[0-9][0-9A-Za-z.+-]*$")


class ArtifactError(RuntimeError):
    pass


class _CredentialSafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


@dataclass(frozen=True)
class ArtifactLock:
    repository: str
    source_sha: str
    accepted_main_sha: str
    release_tag: str
    wheel_filename: str
    wheel_sha256: str
    version: str
    # SDK-only: the major of the HTTP API contract the client speaks. `aios-db`
    # is an in-process library with no wire contract, so it declares none —
    # optional rather than a meaningless `1` that would read as a real claim.
    api_major: int | None = None

    def as_dict(self) -> dict[str, object]:
        return dict(vars(self))

    def with_wheel_sha256(self, value: str) -> ArtifactLock:
        return replace(self, wheel_sha256=value)


def load_lock(path: Path = LOCK_PATH) -> ArtifactLock:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        lock = ArtifactLock(**payload)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise ArtifactError("invalid AIOS SDK lock") from error
    if (
        len(lock.source_sha) != 40
        or len(lock.accepted_main_sha) != 40
        or len(lock.wheel_sha256) != 64
        or (lock.api_major is not None and lock.api_major <= 0)
        or not _RELEASE_TAG_PATTERN.fullmatch(lock.release_tag)
    ):
        raise ArtifactError("invalid AIOS SDK lock identity")
    return lock


def validate_release_metadata(payload: object, lock: ArtifactLock) -> dict[str, int]:
    """Return {asset_name: asset_id} for the wheel and checksum manifest.

    Fails closed unless the payload is the published (non-draft) release for
    exactly the locked tag and carries both required assets.
    """
    if not isinstance(payload, dict):
        raise ArtifactError("invalid release metadata")
    if payload.get("tag_name") != lock.release_tag or payload.get("draft") is not False:
        raise ArtifactError("release metadata identity mismatch")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ArtifactError("release metadata identity mismatch")
    wanted = {lock.wheel_filename, CHECKSUM_MANIFEST_NAME}
    resolved: dict[str, int] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        asset_id = asset.get("id")
        if name in wanted and isinstance(asset_id, int) and asset_id > 0:
            resolved[name] = asset_id
    if set(resolved) != wanted:
        raise ArtifactError("release is missing wheel or checksum asset")
    return resolved


def validate_tag_binding(ref_payload: object, tag_payload: object, lock: ArtifactLock) -> None:
    """Prove the locked tag still points at the accepted main commit.

    ``ref_payload`` is ``git/ref/tags/<tag>``. A lightweight tag points
    straight at the commit; an annotated tag points at a tag object whose
    dereferenced target (``tag_payload``, from ``git/tags/<sha>``) must be
    the accepted commit.
    """
    if not isinstance(ref_payload, dict):
        raise ArtifactError("invalid tag reference metadata")
    obj = ref_payload.get("object")
    if not isinstance(obj, dict):
        raise ArtifactError("invalid tag reference metadata")
    if obj.get("type") == "commit":
        if obj.get("sha") != lock.accepted_main_sha:
            raise ArtifactError("release tag no longer points at the accepted commit")
        return
    if obj.get("type") != "tag":
        raise ArtifactError("invalid tag reference metadata")
    if not isinstance(tag_payload, dict):
        raise ArtifactError("invalid tag object metadata")
    target = tag_payload.get("object")
    if not isinstance(target, dict) or target.get("sha") != lock.accepted_main_sha:
        raise ArtifactError("release tag no longer points at the accepted commit")


def verify_wheel(wheel: bytes, manifest: str, lock: ArtifactLock) -> str:
    digest = hashlib.sha256(wheel).hexdigest()
    manifest_lines = [line for line in manifest.splitlines() if line.strip()]
    expected = f"{digest}  {lock.wheel_filename}"
    if not any(
        line == expected or line.split() == [digest, lock.wheel_filename] for line in manifest_lines
    ):
        raise ArtifactError("artifact manifest checksum mismatch")
    if digest != lock.wheel_sha256:
        raise ArtifactError("locked wheel checksum mismatch")
    return digest


def persist_verified_wheel(wheel: bytes, manifest: str, output: Path, lock: ArtifactLock) -> Path:
    verify_wheel(wheel, manifest, lock)
    output.mkdir(parents=True, exist_ok=True)
    target = output / lock.wheel_filename
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(wheel)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ArtifactError("cannot persist verified SDK wheel") from error
    return target


def _read(request: Request, limit: int) -> bytes:
    try:
        with build_opener(_CredentialSafeRedirect()).open(request, timeout=30) as response:
            data = response.read(limit + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ArtifactError("AIOS SDK artifact download failed") from error
    if len(data) > limit:
        raise ArtifactError("AIOS SDK artifact exceeds size limit")
    return data


def _api_headers(token: str, *, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _read_json(url: str, token: str) -> object:
    raw = _read(
        Request(url, headers=_api_headers(token, accept="application/vnd.github+json")),
        MAX_METADATA_BYTES,
    )
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ArtifactError("invalid release metadata") from error


def fetch_release_wheel(lock: ArtifactLock, token: str) -> tuple[bytes, str]:
    """Download (wheel bytes, checksum manifest text) from the locked release."""
    if not token:
        raise ArtifactError(f"{READONLY_TOKEN_ENV} or {LEGACY_TOKEN_ENV} is required")
    api = f"https://api.github.com/repos/{lock.repository}"

    release = _read_json(f"{api}/releases/tags/{lock.release_tag}", token)
    assets = validate_release_metadata(release, lock)

    ref = _read_json(f"{api}/git/ref/tags/{lock.release_tag}", token)
    tag_object: object = None
    if isinstance(ref, dict):
        obj = ref.get("object")
        if isinstance(obj, dict) and obj.get("type") == "tag":
            tag_object = _read_json(f"{api}/git/tags/{obj.get('sha')}", token)
    validate_tag_binding(ref, tag_object, lock)

    def _asset(name: str, limit: int) -> bytes:
        return _read(
            Request(
                f"{api}/releases/assets/{assets[name]}",
                headers=_api_headers(token, accept="application/octet-stream"),
            ),
            limit,
        )

    wheel = _asset(lock.wheel_filename, MAX_ARTIFACT_BYTES)
    try:
        manifest = _asset(CHECKSUM_MANIFEST_NAME, MAX_METADATA_BYTES).decode("ascii")
    except UnicodeDecodeError as error:
        raise ArtifactError("invalid checksum manifest") from error
    return wheel, manifest


def resolve_artifact_token(env: dict[str, str] | None = None) -> str:
    mapping = os.environ if env is None else env
    token = mapping.get(READONLY_TOKEN_ENV, "")
    if token:
        return token
    legacy = mapping.get(LEGACY_TOKEN_ENV, "")
    if legacy:
        return legacy
    raise ArtifactError(f"{READONLY_TOKEN_ENV} or {LEGACY_TOKEN_ENV} is required")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--lock",
        type=Path,
        default=LOCK_PATH,
        help="artifact lock to fetch (default: aios-sdk.lock.json)",
    )
    args = parser.parse_args()
    lock = load_lock(args.lock)
    wheel, manifest = fetch_release_wheel(lock, resolve_artifact_token())
    path = persist_verified_wheel(wheel, manifest, args.output, lock)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
