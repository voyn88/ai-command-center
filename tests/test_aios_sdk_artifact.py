from __future__ import annotations

import hashlib
import json

import pytest

from scripts.fetch_aios_sdk_artifact import (
    ArtifactError,
    load_lock,
    persist_verified_wheel,
    resolve_artifact_token,
    validate_release_metadata,
    validate_tag_binding,
)


def test_sdk_lock_is_exact_and_contains_no_mutable_or_sibling_fallback():
    lock = load_lock()
    assert lock.repository == "dimastov-lab/aios"
    assert lock.source_sha == "f1f7d90374afa236fe572c77ccc59c99849b6f86"
    assert lock.accepted_main_sha == "f1f7d90374afa236fe572c77ccc59c99849b6f86"
    assert lock.release_tag == "v0.2.2rc3"
    assert lock.wheel_sha256 == "b27882375dc48a7f03864705ff75d51606d67a2d5c8c6b774bd8ad68fbb95ccd"
    rendered = json.dumps(lock.as_dict())
    assert "../aios" not in rendered
    assert '"main"' not in rendered
    assert '"latest"' not in rendered


def test_wheel_persists_only_with_exact_checksum(tmp_path):
    lock = load_lock()
    wheel = b"wheel-bytes"
    checksum = hashlib.sha256(wheel).hexdigest()
    test_lock = lock.with_wheel_sha256(checksum)
    manifest = f"{checksum}  {lock.wheel_filename}\n"
    path = persist_verified_wheel(wheel, manifest, tmp_path, test_lock)
    assert path.read_bytes() == wheel


def test_wheel_persists_with_extra_manifest_lines(tmp_path):
    lock = load_lock()
    wheel = b"wheel-bytes"
    checksum = hashlib.sha256(wheel).hexdigest()
    test_lock = lock.with_wheel_sha256(checksum)
    manifest = (
        "0" * 64 + "  aios-0.2.2rc3-py3-none-any.whl\n"
        f"{checksum}  {lock.wheel_filename}\n"
    )
    path = persist_verified_wheel(wheel, manifest, tmp_path, test_lock)
    assert path.read_bytes() == wheel


def test_checksum_mismatch_fails_closed_without_writing_wheel(tmp_path):
    lock = load_lock()
    manifest = f"{lock.wheel_sha256}  {lock.wheel_filename}\n"
    with pytest.raises(ArtifactError, match="checksum"):
        persist_verified_wheel(b"tampered", manifest, tmp_path, lock)
    assert not (tmp_path / lock.wheel_filename).exists()


def test_manifest_matching_a_tampered_wheel_is_still_rejected_by_the_pinned_lock(tmp_path):
    """A compromised release can republish a malicious wheel together with a
    SHA256SUMS entry that matches it, so manifest self-consistency alone must
    not be enough — the pinned lock.wheel_sha256 has to independently reject
    a wheel it never accepted, even when the manifest agrees with it.
    """
    lock = load_lock()
    wheel = b"malicious-payload"
    manifest = f"{hashlib.sha256(wheel).hexdigest()}  {lock.wheel_filename}\n"
    with pytest.raises(ArtifactError, match="locked wheel checksum mismatch"):
        persist_verified_wheel(wheel, manifest, tmp_path, lock)
    assert not (tmp_path / lock.wheel_filename).exists()


def _release_payload(lock, *, draft: bool = False, tag: str | None = None) -> dict:
    return {
        "tag_name": tag or lock.release_tag,
        "draft": draft,
        "assets": [
            {"name": lock.wheel_filename, "id": 101},
            {"name": "SHA256SUMS", "id": 102},
        ],
    }


def test_release_metadata_binds_tag_and_assets():
    lock = load_lock()
    assets = validate_release_metadata(_release_payload(lock), lock)
    assert assets == {lock.wheel_filename: 101, "SHA256SUMS": 102}


def test_release_metadata_rejects_draft_wrong_tag_and_missing_assets():
    lock = load_lock()
    with pytest.raises(ArtifactError, match="identity"):
        validate_release_metadata(_release_payload(lock, draft=True), lock)
    with pytest.raises(ArtifactError, match="identity"):
        validate_release_metadata(_release_payload(lock, tag="v9.9.9"), lock)
    incomplete = _release_payload(lock)
    incomplete["assets"] = incomplete["assets"][:1]
    with pytest.raises(ArtifactError, match="missing wheel or checksum"):
        validate_release_metadata(incomplete, lock)


def test_tag_binding_accepts_annotated_tag_peeled_to_accepted_commit():
    lock = load_lock()
    ref = {"object": {"type": "tag", "sha": "t" * 40}}
    tag = {"object": {"type": "commit", "sha": lock.accepted_main_sha}}
    validate_tag_binding(ref, tag, lock)


def test_tag_binding_accepts_lightweight_tag_at_accepted_commit():
    lock = load_lock()
    ref = {"object": {"type": "commit", "sha": lock.accepted_main_sha}}
    validate_tag_binding(ref, None, lock)


def test_tag_binding_rejects_moved_or_malformed_tag():
    lock = load_lock()
    moved = {"object": {"type": "commit", "sha": "a" * 40}}
    with pytest.raises(ArtifactError, match="accepted commit"):
        validate_tag_binding(moved, None, lock)
    annotated = {"object": {"type": "tag", "sha": "t" * 40}}
    retargeted = {"object": {"type": "commit", "sha": "a" * 40}}
    with pytest.raises(ArtifactError, match="accepted commit"):
        validate_tag_binding(annotated, retargeted, lock)
    with pytest.raises(ArtifactError, match="invalid tag reference"):
        validate_tag_binding({}, None, lock)


def test_resolve_artifact_token_prefers_readonly_then_legacy():
    assert resolve_artifact_token({"AIOS_ARTIFACT_READONLY_TOKEN": "new", "AIOS_ARTIFACT_READ_TOKEN": "old"}) == "new"
    assert resolve_artifact_token({"AIOS_ARTIFACT_READ_TOKEN": "old"}) == "old"


def test_resolve_artifact_token_requires_one_of_envs():
    with pytest.raises(ArtifactError, match="AIOS_ARTIFACT_READONLY_TOKEN or AIOS_ARTIFACT_READ_TOKEN"):
        resolve_artifact_token({})
