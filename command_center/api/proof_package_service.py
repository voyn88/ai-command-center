"""Service tier for the VOYN-MIN-WOW-1 proof-package surface (routes →
**service** → repository → db).

The route in :mod:`command_center.api.proof_package_routes` holds no logic; it
calls :func:`get_proof_package` and maps the one domain error onto HTTP 400.
This module is the only place that:

* resolves and lazily migrates the runtime db (the repository functions take
  an explicit ``db_path``), the same lazy-migrate pattern every other Wave
  service uses;
* applies the BANK/LEGAL redaction policy: a sensitive project's package is
  never assembled, the same "reject the write/read outright" choice
  :mod:`command_center.api.audit_service` makes for a sensitive audit run —
  there is no partial package to redact down to;
* maps the repository's plain dict onto the
  :mod:`command_center.api.proof_package_schemas` contract.
"""

from __future__ import annotations

from pathlib import Path

from command_center.api import proof_package_schemas as p
from command_center.project_config import is_sensitive
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION

# Repo root is three levels up: <root>/command_center/api/proof_package_service.py
ROOT = Path(__file__).resolve().parents[2]


class SensitiveProjectRefError(Exception):
    """Raised when a proof package is requested for a BANK/LEGAL project. Its
    contents would be redacted from every other surface anyway, so the request
    is rejected (HTTP 400) rather than assembling a package with nothing
    visible in it."""


def _db_path() -> Path:
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


def get_proof_package(project: str) -> p.ProofPackage:
    """Assemble and return the proof package for ``project``. Raises
    :class:`SensitiveProjectRefError` for a BANK/LEGAL project."""
    if is_sensitive(project):
        raise SensitiveProjectRefError(
            f"proof package for sensitive project {project!r} is rejected"
        )
    package = db.build_proof_package(_db_path(), project=project)
    return p.ProofPackage(**package)
