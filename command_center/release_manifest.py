from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_PLATFORMS = {"linux", "macos", "windows", "web", "streamlit", "cross-platform"}
ALLOWED_SIGNING_STATUS = {"unknown", "signed", "unsigned", "blocked", "not_applicable"}


@dataclass(frozen=True)
class ArtifactSpec:
    path: Path
    platform: str
    artifact_type: str
    signing_required: bool
    signing_status: str
    signing_identity: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(path: Path, repository_root: Path) -> str:
    resolved = path.resolve()
    root = repository_root.resolve()
    if resolved == root or root in resolved.parents:
        return resolved.relative_to(root).as_posix()
    return resolved.name


def _validate_spec(raw: dict[str, object], repository_root: Path) -> ArtifactSpec:
    raw_path = raw.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("artifact spec requires non-empty string path")
    file_path = Path(raw_path)
    if not file_path.is_absolute():
        file_path = repository_root / file_path
    if not file_path.exists() or not file_path.is_file():
        raise ValueError(f"artifact path does not exist: {raw_path}")

    platform = raw.get("platform")
    if not isinstance(platform, str) or platform not in ALLOWED_PLATFORMS:
        raise ValueError(f"artifact spec has unsupported platform: {platform!r}")

    artifact_type = raw.get("artifact_type")
    if not isinstance(artifact_type, str) or not artifact_type.strip():
        raise ValueError("artifact spec requires non-empty artifact_type")

    signing_required = bool(raw.get("signing_required", True))
    signing_status = raw.get("signing_status", "unknown")
    if not isinstance(signing_status, str) or signing_status not in ALLOWED_SIGNING_STATUS:
        raise ValueError(f"artifact spec has unsupported signing_status: {signing_status!r}")

    signing_identity = raw.get("signing_identity")
    if signing_identity is not None and not isinstance(signing_identity, str):
        raise ValueError("artifact spec signing_identity must be a string or null")
    if isinstance(signing_identity, str) and not signing_identity.strip():
        signing_identity = None

    return ArtifactSpec(
        path=file_path,
        platform=platform,
        artifact_type=artifact_type.strip(),
        signing_required=signing_required,
        signing_status=signing_status,
        signing_identity=signing_identity,
    )


def build_release_manifest(
    source_sha: str, artifact_specs: list[dict[str, object]], repository_root: Path
) -> dict[str, object]:
    if not SOURCE_SHA_RE.fullmatch(source_sha or ""):
        raise ValueError("source_sha must be a 40-character lowercase git SHA")
    if not artifact_specs:
        raise ValueError("artifact_specs must not be empty")

    parsed = [_validate_spec(spec, repository_root) for spec in artifact_specs]
    items: list[dict[str, object]] = []
    for spec in parsed:
        relative_path = _relative_path(spec.path, repository_root)
        items.append(
            {
                "artifact_type": spec.artifact_type,
                "platform": spec.platform,
                "file_name": spec.path.name,
                "relative_path": relative_path,
                "size_bytes": spec.path.stat().st_size,
                "sha256": _sha256(spec.path),
                "signing": {
                    "required": spec.signing_required,
                    "status": spec.signing_status,
                    "identity": spec.signing_identity,
                },
            }
        )
    items.sort(key=lambda item: (str(item["platform"]), str(item["relative_path"]), str(item["file_name"])))
    return {"schema_version": 1, "source_sha": source_sha, "artifacts": items}
