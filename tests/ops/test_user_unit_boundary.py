"""VOYN-W0-AICC-SRV-05: a user unit is not a boundary.

``deploy/systemd/aicc-worker.service`` (and the other hardened worker units)
carry `ProtectSystem`, `ProtectHome`, `NoNewPrivileges` and friends to fence
the worker in. Those directives are only a real boundary when the unit that
declares them is a *system* unit: root-owned under `/etc/systemd/system`,
started by the system manager, `User=aicc-worker` performing an actual
privilege drop. The account being confined does not hold write access to its
own confinement and cannot edit-and-reload its way out.

A *user* unit inverts that: the account systemd would confine also owns the
unit file (`~/.config/systemd/user/*.service` and friends) and can edit,
disable or `systemctl --user daemon-reload` away any directive in it. The
namespaces and mounts still function the same way under a user manager --
that part of the doctrine's prior framing was wrong -- but with the confined
account holding the pen, the directives stop being enforcement and become a
suggestion to itself.

Two ways a deploy could land a worker unit there without anyone deciding to:

1. The installer stages the unit file itself into a location systemd's user
   manager would load from.
2. Something enables/starts it through the per-user manager
   (`systemctl --user` / `loginctl --user`) regardless of which directory the
   file physically sits in -- `systemctl --user link` never copies anything
   onto a directory allowlist a path-based check would catch.

Both are checked here against the REAL installer output and REAL systemd
unit search paths (queried from the `systemd-analyze` binary itself, not a
hand-maintained list of "marker" directories -- a hand-maintained list is
exactly what went stale before: it missed `$XDG_DATA_HOME/systemd/user` and
the generator/transient directories under `$XDG_RUNTIME_DIR/systemd/`).
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_UNIT_TYPE_SUFFIXES = (
    ".service",
    ".socket",
    ".timer",
    ".mount",
    ".path",
    ".slice",
    ".target",
)

# The one place that decides where every principal-isolation install target
# lands (`deploy/install-agent-principal-isolation.sh` never `cp`s a unit
# file itself -- it only drives this transaction's FileSpec table).
_INSTALL_TRANSACTION = REPO_ROOT / "ops" / "aicc_install_transaction.py"

_UNIT_MANAGER_RE = re.compile(r"\b(?:systemctl|loginctl)\b")
_USER_FLAG_RE = re.compile(r"(?<!-)--user(?!\w)")


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _default_specs(module):
    return module.default_specs(
        REPO_ROOT,
        authority_env=Path("/etc/aicc/workspace-authority.env"),
        claude_auth=Path("/var/lib/aicc-agent/claude/.claude/.credentials.json"),
        codex_auth=Path("/var/lib/aicc-agent/codex/.codex/auth.json"),
        resolve_identities=False,
    )


def _is_unit_load_path_entry(target: str) -> bool:
    """True for a unit file itself, or a drop-in `.conf` inside a
    `<name>.<type>.d/` override directory -- both are entries systemd's own
    unit loader resolves from its search path, so both must resolve under a
    SYSTEM search directory."""
    path = Path(target)
    if path.suffix in _UNIT_TYPE_SUFFIXES:
        return True
    if path.suffix == ".conf" and path.parent.suffix == ".d":
        stem = path.parent.name[: -len(".d")]
        return stem.endswith(_UNIT_TYPE_SUFFIXES)
    return False


def _unit_search_dirs(user: bool) -> list[Path]:
    """The authoritative unit load path for a hermetic, representative
    account -- queried from the real systemd binary so the result reflects
    whatever paths that binary actually searches, not a list this test
    maintains by hand. A synthetic HOME/XDG_RUNTIME_DIR is supplied so the
    full canonical set is reported even when the test itself runs outside a
    real login session."""
    if not any((Path(p) / "systemd-analyze").exists() for p in ("/usr/bin", "/bin")):
        pytest.skip("systemd-analyze is not installed on this host")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/aicc-boundary-probe",
        "XDG_RUNTIME_DIR": "/run/user/999999",
    }
    args = ["systemd-analyze", *(["--user"] if user else []), "unit-paths"]
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=20, env=env, check=False
    )
    if result.returncode != 0:
        pytest.skip(f"systemd-analyze unit-paths failed: {result.stderr.strip()}")
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def _under_any(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _install_section_wantedby(unit_text: str) -> list[str]:
    """WantedBy= values scoped to the `[Install]` section only -- a real,
    section-aware parse (ignoring `#`/`;` comments and anything outside the
    section) rather than a whole-file substring search, which would accept
    `WantedBy=multi-user.target` sitting in a comment or malformed trailing
    content that never actually reaches the `[Install]` section."""
    section = None
    targets: list[str] = []
    for raw_line in unit_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section == "Install" and "=" in line:
            key, _, value = line.partition("=")
            if key.strip() == "WantedBy":
                targets.extend(value.split())
    return targets


def test_principal_isolation_installer_stages_units_only_under_a_system_directory():
    """Exercises `default_specs()` for its real resolved destinations -- not
    a grep of source text for path literals, which a dynamically-built path
    could dodge -- and checks each unit-load-path entry against both the
    real system and real user search paths."""
    module = _load_module(_INSTALL_TRANSACTION)
    specs = _default_specs(module)
    system_dirs = _unit_search_dirs(user=False)
    user_dirs = _unit_search_dirs(user=True)

    unit_targets = [spec.target for spec in specs if _is_unit_load_path_entry(spec.target)]
    assert unit_targets, "expected default_specs() to install at least one unit-type file"

    violations = [
        target
        for target in unit_targets
        if not _under_any(Path(target), system_dirs) or _under_any(Path(target), user_dirs)
    ]
    assert not violations, (
        "installer stages a systemd unit-load-path entry outside the system "
        f"unit search path (or inside the user one): {violations}"
    )


def test_recovery_generator_anchor_is_a_system_generator():
    """The permanent boot-recovery generator (installed before any other
    mutation, per `default_specs()`/`RECOVERY_ANCHOR_TARGET`) must live in
    the SYSTEM generator directory -- `/usr/lib/systemd/user-generators/`
    exists as a distinct, real location and would silently make this a
    per-user bootstrap instead."""
    module = _load_module(_INSTALL_TRANSACTION)
    anchor = Path(module.RECOVERY_ANCHOR_TARGET)
    assert "system-generators" in anchor.parts
    assert "user-generators" not in anchor.parts


@pytest.mark.parametrize(
    "unit_path",
    sorted(
        p
        for p in (REPO_ROOT / "deploy" / "systemd").iterdir()
        if p.suffix in (".service", ".socket", ".timer")
    ),
    ids=lambda p: p.name,
)
def test_deploy_systemd_unit_install_section_names_only_system_targets(unit_path):
    """Every shipped unit that declares `[Install]` must want a real
    SYSTEM-manager target. `default.target`/`graphical-session.target` are
    the user-manager's own special targets and naming either here is the
    first sign a unit is meant to run under `systemctl --user` instead."""
    allowed = frozenset({"multi-user.target", "timers.target", "sockets.target"})
    targets = _install_section_wantedby(unit_path.read_text(encoding="utf-8"))
    if not targets:
        pytest.skip(f"{unit_path.name} declares no [Install] section (template/drop-in)")
    unexpected = [t for t in targets if t not in allowed]
    assert not unexpected, (
        f"{unit_path.name}: [Install] WantedBy names a non-system-manager "
        f"target {unexpected} -- allowed: {sorted(allowed)}"
    )


@pytest.mark.parametrize(
    "unit_path",
    sorted((REPO_ROOT / "deploy" / "systemd").iterdir()),
    ids=lambda p: p.name,
)
def test_deploy_systemd_source_never_mentions_a_user_unit_path(unit_path):
    """Belt-and-braces on the committed unit sources themselves: none of
    them has any legitimate reason to mention a per-user systemd directory
    anywhere in comments or install instructions."""
    if not unit_path.is_file():
        pytest.skip("not a regular file")
    text = unit_path.read_text(encoding="utf-8")
    assert "systemd/user" not in text
    assert ".config/systemd" not in text
    assert "--user" not in text


def _tracked_files(pattern: str) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", pattern],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


def _production_scan_targets() -> list[Path]:
    files = _tracked_files("*.py") + _tracked_files("*.sh")
    this_file = Path(__file__).resolve()
    return [
        path
        for path in files
        if path.resolve() != this_file and "tests/" not in path.relative_to(REPO_ROOT).as_posix()
    ]


def test_no_production_script_enables_or_starts_a_unit_through_the_user_manager():
    """`systemctl --user` / `loginctl --user` would install, enable or start
    a unit through the PER-USER manager regardless of which directory the
    backing file sits in -- `systemctl --user link` never copies the file
    onto any destination-based allowlist. Scanned as a whole-file
    co-occurrence (not a single-line regex) so a multi-line or
    dynamically-assembled argv list can't dodge it: the literal token
    `--user` must appear in the source text of a file that also invokes
    `systemctl`/`loginctl` for this to ever fire, and today none does."""
    violations = []
    for path in _production_scan_targets():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _UNIT_MANAGER_RE.search(text) and _USER_FLAG_RE.search(text):
            violations.append(str(path.relative_to(REPO_ROOT)))
    assert not violations, (
        "systemctl/loginctl invoked with --user in production code -- this "
        f"targets the per-user manager, never the system one: {violations}"
    )
