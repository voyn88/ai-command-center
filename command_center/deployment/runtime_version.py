"""Report the loaded owner application's immutable release identity.

The existing self-deployer writes the marker before atomically publishing the
release directory. This is deployment provenance, not a signature or proof
against an operator who can modify release files. Mutable checkout deployments
cannot provide this identity and must report unknown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeVersion:
    sha: str | None


def capture_runtime_version(module_file: Path) -> RuntimeVersion:
    """Capture once at application creation using the import-resolved module path.

    Do not resolve a mutable ``current`` selector here: the caller captures its
    physical module path at import, before a later deployment can switch it.
    Neither environment variables nor the source clone's HEAD are evidence of
    the code already loaded by this process.
    """
    try:
        if (not module_file.is_absolute()
                or module_file.parts[-3:] != ('command_center', 'webapi', 'owner_app.py')):
            return RuntimeVersion(None)
        release = module_file.parents[2]
        sha = release.name
        if release.parent.name != 'releases' or not re.fullmatch(r'[0-9a-f]{40}', sha):
            return RuntimeVersion(None)
        # Require the already-resolved physical path, never a mutable selector.
        if module_file.resolve(strict=True) != module_file:
            return RuntimeVersion(None)
        marker = release / '.aicc-release-sha'
        if marker.is_symlink() or not marker.is_file():
            return RuntimeVersion(None)
        with marker.open('rb') as stream:
            raw = stream.read(42)
        if raw not in (sha.encode('ascii'), (sha + '\n').encode('ascii')):
            return RuntimeVersion(None)
        return RuntimeVersion(sha)
    except (OSError, RuntimeError, ValueError):
        return RuntimeVersion(None)
