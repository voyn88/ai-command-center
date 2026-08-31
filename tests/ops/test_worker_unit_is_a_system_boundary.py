"""VOYN-W0-AICC-SRV-05-USER-UNIT-IS-NOT-A-BOUNDARY.

The worker must be supervised as a systemd *system* unit -- installed root-owned
under a real system search path and started by the system manager -- never as a
per-user unit a developer's own login session could start in "dev mode". Three
rounds of adversarial review rejected earlier attempts at this:

  1. A test asserting `WantedBy=multi-user.target` as a substring never looked
     at the installer or at where a unit actually lands on disk.
  2. A deny-list of "user unit directories" it checked destinations against was
     not exhaustive (it missed $XDG_DATA_HOME/systemd/user and
     $XDG_RUNTIME_DIR/systemd, among others -- that set is open-ended).
  3. A grep for the manager name and a `--user` flag, restricted to tracked
     `*.py`/`*.sh` files, claimed to mechanically prove no script could ever
     start a unit through the user manager. That claim was false: a non-.py/.sh
     production file, or dynamically assembled argv, both dodge a text scan.

This file does not repeat any of those. The actual guarantee is enforced in
`ops/aicc_install_transaction.py::FileTransaction._target`, which refuses to
resolve an installation target shaped like a unit or unit drop-in unless its
directory is one of the real system-unit search paths
(`SYSTEM_UNIT_DIRECTORIES`) -- an allow-list of systemd's own documented
directories, not a deny-list of every place a user unit could hide, and
enforced on the concrete destination string at the moment a file would be
written, so it cannot be dodged by how that string was assembled upstream.
What follows tests that enforcement is real (by driving the actual installer
function and the actual `FileTransaction`, not by pattern-matching unit text),
and adds a closure-style, extension-agnostic regression fence for `systemctl`
call sites -- which is honest about what it can and cannot prove; see
`test_no_untracked_systemctl_reference_escapes_the_reviewed_allowlist` below.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = REPO_ROOT / "ops" / "aicc_install_transaction.py"
    spec = importlib.util.spec_from_file_location("aicc_install_transaction", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _spec(module, source: Path, target: str, mode: int = 0o644):
    import os

    return module.FileSpec(source, target, mode, os.geteuid(), os.getegid())


def _default_specs(module, tmp_path: Path, *, profile: str = "worker"):
    return module.default_specs(
        REPO_ROOT,
        authority_env=tmp_path / "authority.env",
        claude_auth=tmp_path / "claude.json",
        codex_auth=tmp_path / "codex.json",
        resolve_identities=False,
        profile=profile,
    )


@pytest.mark.parametrize("profile", ["worker", "control"])
def test_default_specs_confine_every_systemd_unit_to_a_system_directory(
    tmp_path, profile
):
    """The real installer manifest, not a copy of it, is what gets checked."""
    module = _module()
    specs = _default_specs(module, tmp_path, profile=profile)

    unit_specs = [
        spec for spec in specs if module._unit_root_directory(spec.target) is not None
    ]
    if profile == "worker":
        # A vacuous pass (no unit-shaped target found at all) would be as
        # useless as the deny-list this replaces; the worker's own template
        # unit and its principal-isolation drop-in must both show up here.
        assert len(unit_specs) >= 4, unit_specs
    else:
        # `control` drops every worker-only target, which today includes
        # every unit this installer ships -- so an empty list here is the
        # documented, correct shape, not a vacuous check: the `worker` case
        # above already proves the classifier finds real units when they are
        # present, and `test_worker_only_targets_include_every_shipped_unit`
        # below proves WORKER_ONLY_TARGETS is exactly why they disappear here.
        assert unit_specs == []

    for spec in unit_specs:
        unit_root = module._unit_root_directory(spec.target)
        assert unit_root in module.SYSTEM_UNIT_DIRECTORIES, (
            spec.target,
            unit_root,
        )


@pytest.mark.parametrize(
    "sneaky_target",
    [
        # $XDG_CONFIG_HOME default -- the classic per-user unit tree.
        "/home/dev/.config/systemd/user/voyn-aicc-worker@.service",
        # $XDG_DATA_HOME default -- missed by the deny-list in the prior round.
        "/home/dev/.local/share/systemd/user/voyn-aicc-worker@.service",
        # $XDG_RUNTIME_DIR -- also missed by the prior deny-list.
        "/run/user/1000/systemd/user/voyn-aicc-worker@.service",
        # The system-wide (but still user-manager-scoped) unit trees.
        "/etc/systemd/user/voyn-aicc-worker@.service",
        "/usr/lib/systemd/user/voyn-aicc-worker@.service",
        "/usr/local/lib/systemd/user/voyn-aicc-worker@.service",
        # A drop-in staged into a user tree, not a bare unit -- the same
        # evasion applied to an override directory instead of the unit file.
        "/home/dev/.config/systemd/user/voyn-aicc-worker@.service.d/20-evil.conf",
        # A path that merely borrows systemd naming without being a real
        # search path at all.
        "/opt/not-a-real-systemd-tree/aicc-worker.service",
    ],
)
def test_installer_refuses_to_stage_a_unit_outside_a_system_unit_directory(
    tmp_path, sneaky_target
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    source = tmp_path / "source"
    source.write_bytes(b"planted unit\n")

    transaction = module.FileTransaction(root, state)
    with pytest.raises(ValueError, match="system unit directory"):
        transaction.prepare((_spec(module, source, sneaky_target),))

    # Refusal happens during preflight: nothing was written, no journal
    # exists, and the state directory was never even claimed for a
    # transaction.
    assert not transaction.pending.exists()
    assert not (root / sneaky_target.lstrip("/")).exists()


def test_installer_still_accepts_the_real_worker_units_after_the_guard(tmp_path):
    """The guard must not be so broad it breaks the installer it protects."""
    import os

    module = _module()
    specs = _default_specs(module, tmp_path, profile="worker")
    unit_specs = [
        spec for spec in specs if module._unit_root_directory(spec.target) is not None
    ]
    assert unit_specs
    # default_specs hardcodes root:root ownership for a real install; running
    # this unprivileged, so re-wrap each spec with this process's own uid/gid
    # -- the guard runs on `target`, never on uid/gid, so this changes nothing
    # about what is under test.
    unit_specs = [
        module.FileSpec(
            spec.source, spec.target, spec.mode, os.geteuid(), os.getegid()
        )
        for spec in unit_specs
    ]

    root = tmp_path / "root"
    state = tmp_path / "state"
    transaction = module.FileTransaction(root, state)
    transaction.prepare(unit_specs)
    transaction.apply()

    for spec in unit_specs:
        installed = root / spec.target.lstrip("/")
        assert installed.is_file()
        assert installed.read_bytes() == Path(spec.source).read_bytes()


def test_worker_only_targets_include_every_shipped_unit(tmp_path):
    """Every unit-shaped install target is gated behind the worker profile.

    `control` (checked above) is only a safe, non-vacuous empty list because
    of this: WORKER_ONLY_TARGETS is exactly the set of unit-shaped targets
    default_specs ever produces, so dropping it removes them all rather than
    happening to remove all of them today.
    """
    module = _module()
    worker_specs = _default_specs(module, tmp_path, profile="worker")
    unit_targets = {
        spec.target
        for spec in worker_specs
        if module._unit_root_directory(spec.target) is not None
    }
    worker_only_unit_targets = {
        target
        for target in module.WORKER_ONLY_TARGETS
        if module._unit_root_directory(target) is not None
    }
    assert unit_targets == worker_only_unit_targets
    assert unit_targets, "no shipped unit is worker-only -- profile check is stale"


def _run_recorder():
    """A fake `run` where every unit is proven loaded, active, enabled, with a
    non-zero MainPID -- i.e. every probe this module can issue reports the
    "healthy, already in the desired state" answer, so whichever function
    drives it runs to completion rather than raising on a probe mismatch.
    Argument shape (never business-logic values) is what this file checks.
    """
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        joined = " ".join(argv)
        if "LoadState" in joined:
            return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
        if "MainPID" in joined:
            return SimpleNamespace(returncode=0, stdout="1234\n", stderr="")
        if len(argv) > 1 and argv[1] == "is-active":
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if len(argv) > 1 and argv[1] == "is-enabled":
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return calls, run


def _assert_never_user_scoped(calls: list[list[str]]):
    assert calls, "no systemctl invocation was captured at all"
    for argv in calls:
        assert argv[0] == "/usr/bin/systemctl", argv
        assert "--user" not in argv, argv
        assert "--global" not in argv, argv


def test_restore_service_snapshot_never_invokes_the_user_manager(tmp_path):
    """Fake `run` mirrors the repo's own
    `test_boot_recovery_restores_dynamic_worker_and_auxiliary_unit_state`,
    which is already proven correct for this module's probe sequence; the
    only thing added here is the argv-shape assertion."""
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@blue.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    },
                    "aicc-agent-launcher.socket": {
                        "exists": True,
                        "enabled": False,
                        "active": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        action = command[1]
        unit = command[2] if len(command) > 2 else ""
        stdout = ""
        if action == "is-active":
            stdout = "active\n" if unit.startswith("voyn-aicc-worker") else "inactive\n"
        elif action == "is-enabled":
            stdout = (
                "enabled\n" if unit.startswith("voyn-aicc-worker") else "disabled\n"
            )
        elif action == "show":
            stdout = (
                "loaded\n"
                if "LoadState" in " ".join(command)
                else ("1234\n" if unit.startswith("voyn-aicc-worker") else "0\n")
            )
        return SimpleNamespace(returncode=0, stderr="", stdout=stdout)

    module.restore_service_snapshot(snapshot, run=run)
    _assert_never_user_scoped(calls)


def test_quiesce_service_snapshot_never_invokes_the_user_manager(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@blue.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        joined = " ".join(argv)
        if "LoadState" in joined:
            return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
        if argv[1] == "stop":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "ActiveState" in joined:
            return SimpleNamespace(returncode=0, stdout="inactive\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="0\n", stderr="")

    module.quiesce_service_snapshot(snapshot, run=run)
    _assert_never_user_scoped(calls)


def test_verify_service_snapshot_closure_never_invokes_the_user_manager(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@blue.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls, run = _run_recorder()
    module.verify_service_snapshot_closure(snapshot, run=run)
    _assert_never_user_scoped(calls)


# Every tracked, non-test, non-doc file already known to reference the
# systemd manager at all. Reviewed by hand: none of them passes a user-scope
# flag (proven above for the three call sites that accept an injectable `run`,
# and by direct reading for the rest -- `deploy/voyn-aicc-rotation-helper`'s
# argv is a closed verb allow-list, `deploy/install-agent-principal-isolation.sh`
# and the unit files only ever call the bare system manager or reference it in
# an install-instruction comment).
_REVIEWED_SYSTEMCTL_REFERENCES = frozenset(
    {
        "command_center/deployment/__init__.py",
        "command_center/deployment/self_deploy.py",
        "command_center/ops/credential_rotation.py",
        "deploy/install-agent-principal-isolation.sh",
        "deploy/systemd/aicc-backlog-planner.service",
        "deploy/systemd/aicc-queue-reaper.service",
        "deploy/systemd/aicc-worker.service",
        "deploy/systemd/aicc-worktree-prune.service",
        "deploy/systemd/voyn-aicc-worker@.service",
        "deploy/voyn-aicc-rotation-helper",
        "ops/aicc_agent_launcher.py",
        "ops/aicc_install_transaction.py",
        "ops/aicc_staged_worker_rollout.py",
        "ops/verify-agent-principal-boundary.sh",
    }
)


def test_no_untracked_systemctl_reference_escapes_the_reviewed_allowlist():
    """Extension-agnostic regression fence for new systemd-manager call sites.

    The prior round's check only scanned tracked `*.py`/`*.sh` files, which
    misses exactly the kind of file this repo already has one of --
    `deploy/voyn-aicc-rotation-helper` is a production script with no
    extension at all. This scans every tracked file regardless of extension
    (excluding tests and docs, which discuss the manager without invoking it)
    and requires each one that mentions it to already be on the reviewed list
    above, so a brand-new call site anywhere in the tree fails this test
    until a human adds it here deliberately.

    This is a regression fence, not a proof: it cannot catch a call site that
    spells "systemctl" via string concatenation instead of a literal, because
    nothing textual can. That is exactly why the load-bearing guarantee for
    this ticket lives in code (`FileTransaction._target`'s allow-list check),
    not in this scan -- the concrete destination string is checked at the
    moment of use, after any such concatenation has already happened.
    """
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        check=True,
        text=True,
    )
    tracked = [line for line in listing.stdout.splitlines() if line]
    assert tracked

    offenders = []
    for relative in tracked:
        if relative.startswith("tests/") or relative.startswith("docs/"):
            continue
        if relative.endswith(".md"):
            continue
        path = REPO_ROOT / relative
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "systemctl" in content and relative not in _REVIEWED_SYSTEMCTL_REFERENCES:
            offenders.append(relative)

    assert offenders == [], (
        "new systemd-manager reference(s) outside the reviewed allowlist -- "
        "add to _REVIEWED_SYSTEMCTL_REFERENCES only after confirming the "
        f"call site never passes --user or --global: {offenders}"
    )


def test_reviewed_systemctl_files_never_reference_a_user_scope_flag():
    """None of the reviewed files may gain a `--user`/`--global` flag on an
    executable line later. Comments are skipped: both reviewed files already
    carry prose that mentions an unrelated `--global` (`npm install --global`),
    and flagging comment text would make this fence impossible to keep green
    without being any more protective against a real scoped invocation.
    """
    import re

    user_scope = re.compile(r"--user\b|--global\b")
    offenders = []
    for relative in sorted(_REVIEWED_SYSTEMCTL_REFERENCES):
        content = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.strip().startswith("#"):
                continue
            if user_scope.search(line):
                offenders.append((relative, line.strip()))

    assert offenders == [], offenders
