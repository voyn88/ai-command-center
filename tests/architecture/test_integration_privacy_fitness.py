"""AICC-INT-001 privacy fitness gate: this repository is public.

The Integration Center's registry *contents* — the operator's real project
names, machine-local paths and remotes — are runtime-local configuration
(``data/integration_registry.json``, gitignored) and must never appear in
committed code, docs or tests. This gate scans the integration module and
its docs/tests for the strings that leaked once (a home-directory path and
internal project names) so the mistake cannot recur silently.

Scoped to the integration surface deliberately: some of these tokens exist
legitimately elsewhere (e.g. ``models.PROJECT_IDS`` entries), so a
repo-wide grep would false-positive; the rule here is that the *integration
feature* enumerates nothing private.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Files that make up the Integration Center surface (code, docs, tests).
SCOPED_PATHS = (
    "command_center/integration",
    "command_center/ui/integration_center.py",
    "command_center/api",
    "command_center/design",
    "command_center/digest",
    "command_center/advisor",
    "command_center/conflicts",
    "command_center/council",
    "command_center/audit",
    "command_center/dispatch",
    "command_center/marketplace",
    "command_center/networking",
    "command_center/task_ordering.py",
    "command_center/ui/task_dependencies.py",
    "command_center/events",
    "command_center/ui/operator_dashboard.py",
    "docs/INTEGRATION_CENTER.md",
    "tests/test_integration_registry.py",
    "tests/test_integration_collectors.py",
    "tests/test_integration_center_ui.py",
    "tests/test_digest_service.py",
    "tests/test_owner_autofill.py",
    "tests/test_conflict_db.py",
    "tests/test_conflict_intake.py",
    "tests/test_council_db.py",
    "tests/test_council_intake.py",
    "tests/test_networking_db.py",
    "tests/api/test_conflict_endpoints.py",
    "tests/api/test_council_endpoints.py",
    "tests/api/test_networking_endpoints.py",
    "tests/api/test_digest_ownerday_endpoints.py",
    "tests/dispatch",
    "tests/webapi/test_dispatch_endpoints.py",
    "tests/architecture/test_integration_privacy_fitness.py",
)

#: Strings that must never appear in the scoped files. Split-joined so this
#: gate does not trip over its own source.
FORBIDDEN = (
    "/Users/" + "dmitrijcernikov",
    "AML-" + "Governance",
    "AI" + "COS",
    "dimastov" + "-lab",
)


def _scoped_files() -> list[Path]:
    files: list[Path] = []
    for rel in SCOPED_PATHS:
        path = REPO_ROOT / rel
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.exists():
            files.append(path)
    return files


def test_integration_surface_contains_no_private_names_or_paths():
    files = _scoped_files()
    assert files, "scope resolved to no files — update SCOPED_PATHS"
    violations = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                violations.append(f"{path.relative_to(REPO_ROOT)}: contains {needle!r}")
    assert not violations, (
        "private project names/paths in the public integration surface — "
        f"move them into the gitignored registry store instead: {violations}"
    )


def test_registry_store_is_gitignored():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/integration_registry.json" in gitignore
