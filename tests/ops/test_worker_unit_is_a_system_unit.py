"""A systemd *user* unit is not this project's security boundary.

Both worker unit families (`aicc-worker.service`, the single-lane SRV-05
unit, and `voyn-aicc-worker@.service`, the preprod lane template) declare a
mount/network sandbox (`NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome`, `PrivateDevices`, ...). Those directives still take
mechanical effect in a user unit -- namespaces do not need a privilege
transition to work. What is lost is the *boundary*: a user unit
(`~/.config/systemd/user`, run via `systemctl --user`) is loaded by the
invoking account's own systemd instance, so that same account can edit,
reload, or bypass it at will, and the sandbox then only ever restricts a
process the account owner already fully controls by other means.

`WantedBy=multi-user.target` is the mechanical, un-fakeable signal that a
unit targets the system manager -- `multi-user.target` does not exist in a
user manager, whose own boot target is `default.target`. That line alone is
only installation metadata, though: a file can carry it and still be copied
into a user-unit directory and started explicitly. The tests below also
drive the real installer (`ops/aicc_install_transaction.py`, invoked by
`deploy/install-agent-principal-isolation.sh`) end-to-end against a
throwaway root and read back what it actually wrote, and sweep the
installer scripts for any path that would stage a unit into a systemd user
directory (VOYN-W0-AICC-SRV-05-USER-UNIT-IS-NOT-A-BOUNDARY;
independent-review REJECT on ee5f958 -- the previous version of this test
only substring-matched `WantedBy=multi-user.target` in the unit text and
never examined the installer or its resulting destination).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]

WORKER_UNITS = (
    "deploy/systemd/aicc-worker.service",
    "deploy/systemd/voyn-aicc-worker@.service",
)

# Directory shapes a systemd *user* manager loads units from. None of these
# strings should ever appear in an installer path for a worker unit.
USER_UNIT_DIR_MARKERS = (
    ".config/systemd/user",
    "/etc/systemd/user/",
    "/usr/lib/systemd/user/",
    "/usr/local/lib/systemd/user/",
)


def _install_transaction_module():
    path = ROOT / "ops" / "aicc_install_transaction.py"
    spec = importlib.util.spec_from_file_location("aicc_install_transaction", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse_unit_sections(text: str) -> dict[str, list[tuple[str, str]]]:
    """Read an INI-like unit file the way systemd's own parser does: comments
    and blank lines are skipped, and every directive is attributed to the
    section it actually falls under -- so a directive quoted in a comment,
    or trailing content after the last real section, is never mistaken for
    the real thing.
    """
    sections: dict[str, list[tuple[str, str]]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        sections[current].append((key.strip(), value.strip()))
    return sections


def _directive_values(text: str, section: str, key: str) -> list[str]:
    sections = _parse_unit_sections(text)
    return [value for k, value in sections.get(section, []) if k == key]


@pytest.mark.parametrize("relative_path", WORKER_UNITS)
def test_worker_unit_targets_the_system_manager(relative_path: str) -> None:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    assert _directive_values(text, "Install", "WantedBy") == ["multi-user.target"]
    # default.target is the user manager's own boot target; its presence
    # here would mean the unit is meant to double as a user unit too.
    assert _directive_values(text, "Install", "WantedBy") != ["default.target"]


@pytest.mark.parametrize("relative_path", WORKER_UNITS)
def test_worker_unit_declares_a_privilege_transition(relative_path: str) -> None:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    # `User=` is only a real UID drop when the unit is loaded by the
    # root-owned system manager; asserting it alongside `NoNewPrivileges`
    # ties the two together mechanically. The lane template
    # (voyn-aicc-worker@.service) deliberately carries only
    # NoNewPrivileges itself -- the rest of its mount/network sandbox
    # arrives solely via the principal-isolation drop-in, checked below, so
    # the fail-closed flag can never ship pre-armed in the base unit.
    assert _directive_values(text, "Service", "User") == ["aicc-worker"]
    assert _directive_values(text, "Service", "NoNewPrivileges")


def test_base_worker_unit_carries_its_own_mount_network_sandbox() -> None:
    text = (ROOT / "deploy/systemd/aicc-worker.service").read_text(encoding="utf-8")
    service = dict(_parse_unit_sections(text).get("Service", []))
    for directive, expected in (
        ("NoNewPrivileges", "true"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "true"),
    ):
        assert service.get(directive) == expected, (
            f"aicc-worker.service dropped {directive!r}"
        )


def test_lane_worker_sandbox_arrives_only_via_the_isolation_drop_in() -> None:
    template = (ROOT / "deploy/systemd/voyn-aicc-worker@.service").read_text(
        encoding="utf-8"
    )
    dropin = (
        ROOT / "deploy/systemd/voyn-aicc-worker-principal-isolation.conf"
    ).read_text(encoding="utf-8")
    template_service = dict(_parse_unit_sections(template).get("Service", []))
    dropin_service = dict(_parse_unit_sections(dropin).get("Service", []))
    for directive in ("ProtectSystem", "ProtectHome"):
        assert directive not in template_service, (
            f"voyn-aicc-worker@.service must not pre-arm {directive!r} outside "
            "the drop-in"
        )
        assert directive in dropin_service, f"drop-in dropped {directive!r}"


def test_base_worker_unit_documents_the_user_unit_hazard() -> None:
    text = (ROOT / "deploy/systemd/aicc-worker.service").read_text(encoding="utf-8")
    assert "/etc/systemd/system" in text
    assert "never `systemctl --user`" in text
    assert "user unit is not this boundary" in text


def test_lane_worker_unit_documents_the_user_unit_hazard() -> None:
    text = (ROOT / "deploy/systemd/voyn-aicc-worker@.service").read_text(
        encoding="utf-8"
    )
    assert "/etc/systemd/system" in text
    assert "user unit" in text


def test_drop_in_documents_the_user_unit_hazard() -> None:
    text = (
        ROOT / "deploy/systemd/voyn-aicc-worker-principal-isolation.conf"
    ).read_text(encoding="utf-8")
    assert "/etc/systemd/system" in text
    assert "user unit" in text


def test_base_worker_unit_install_instructions_target_the_system_manager() -> None:
    text = (ROOT / "deploy/systemd/aicc-worker.service").read_text(encoding="utf-8")
    # aicc-worker.service has no scripted installer -- its documented `cp`
    # is the only install path, so it is pinned literally rather than left
    # to the prose around it.
    assert "cp deploy/systemd/aicc-worker.service /etc/systemd/system/" in text


def test_installer_places_lane_worker_files_under_the_system_manager(tmp_path) -> None:
    """The lane template and both principal-isolation drop-ins have a real,
    scripted installer. Drive it end-to-end against a throwaway root and
    read back the files it actually wrote, instead of trusting the
    FileSpec.target strings alone.
    """
    module = _install_transaction_module()
    specs = module.default_specs(
        ROOT,
        authority_env=tmp_path / "authority.env",
        claude_auth=tmp_path / "claude.json",
        codex_auth=tmp_path / "codex.json",
        resolve_identities=False,
    )
    worker_specs = tuple(
        spec
        for spec in specs
        if "/systemd/system/" in spec.target and "worker" in spec.target
    )
    assert len(worker_specs) == 3
    for spec in worker_specs:
        assert spec.target.startswith("/etc/systemd/system/")
        for marker in USER_UNIT_DIR_MARKERS:
            assert marker not in spec.target

    # default_specs() always fills in uid/gid 0 (the real installer runs as
    # root); swap in this process's own ids so the write below can succeed
    # without root.
    runnable_specs = tuple(
        replace(spec, uid=os.geteuid(), gid=os.getegid()) for spec in worker_specs
    )
    root = tmp_path / "root"
    state = tmp_path / "state"
    transaction = module.FileTransaction(root, state)
    transaction.install(runnable_specs)

    installed = {
        spec.target: (root / spec.target.lstrip("/")).read_text(encoding="utf-8")
        for spec in runnable_specs
    }
    assert installed["/etc/systemd/system/voyn-aicc-worker@.service"] == (
        ROOT / "deploy/systemd/voyn-aicc-worker@.service"
    ).read_text(encoding="utf-8")
    dropin_text = (
        ROOT / "deploy/systemd/voyn-aicc-worker-principal-isolation.conf"
    ).read_text(encoding="utf-8")
    assert (
        installed[
            "/etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf"
        ]
        == dropin_text
    )
    assert (
        installed[
            "/etc/systemd/system/aicc-worker.service.d/20-principal-isolation.conf"
        ]
        == dropin_text
    )


def test_no_installer_path_stages_a_unit_into_a_user_unit_directory() -> None:
    """A mechanical sweep, not just a pin on the two known installers: no
    file under deploy/, ops/, or scripts/ should ever reference a systemd
    *user* unit directory. Zero legitimate hits exist today; this fails the
    moment one is introduced.
    """
    hits: list[tuple[Path, str]] = []
    for directory in ("deploy", "ops", "scripts"):
        base = ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for marker in USER_UNIT_DIR_MARKERS:
                if marker in text:
                    hits.append((path, marker))
    assert not hits, f"found systemd user-unit-directory references: {hits}"
