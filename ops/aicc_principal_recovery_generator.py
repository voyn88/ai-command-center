#!/usr/bin/python3
"""Install an early, fail-closed barrier for principal-isolation recovery.

The generator is a permanent bootstrap anchor. It always emits an early unit,
even when no journal is visible while generators run (for example when
``/var`` is a separate filesystem). The oneshot re-enters this file after
local filesystems are mounted, validates the durable journal, and execs the
self-contained recovery capsule recorded by that journal.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path("/var/lib/aicc-principal-isolation")
ANCHOR = Path("/usr/lib/systemd/system-generators/aicc-principal-recovery")
RECOVERY_UNIT = "aicc-principal-recovery.service"
#: The second half of boot recovery. The barrier above runs before
#: sysinit.target and therefore cannot synchronously start a worker that
#: depends on it -- it can only enqueue those starts. Enqueueing is not
#: starting, so this unit runs after the boot has settled and proves the
#: queued jobs actually reached `active`, failing loudly if they did not
#: (independent review on 988de49).
COMPLETION_UNIT = "aicc-principal-recovery-complete.service"
#: Written by `restore_service_snapshot(defer_starts=True)` in
#: ops/aicc_install_transaction.py. That module cannot be imported from here
#: (this file is a systemd generator, executed by PID 1 before anything else
#: is installed), so the name, version and unit pattern are duplicated and
#: held in lockstep by tests/ops/test_aicc_install_transaction.py.
DEFERRED_STARTS = "deferred-starts.json"
DEFERRED_STARTS_VERSION = 1
DEFERRED_UNIT_RE = re.compile(
    r"(?:voyn-aicc-worker@[^/@\s]+\.service|"
    r"voyn-aicc-worker(?:-2)?\.service|"
    r"aicc-worker\.service|"
    r"aicc-agent-launcher@[^/@\s]+\.service|"
    r"aicc-agent-launcher\.socket|aicc-principal-recovery\.service)"
)
CLAIMERS = (
    "aicc-agent-launcher.socket",
    "aicc-agent-launcher@.service",
    "aicc-worker.service",
    "voyn-aicc-worker.service",
    "voyn-aicc-worker-2.service",
    "voyn-aicc-worker@.service",
)
AUXILIARY_JOURNALS = (
    "sensitive-retirement.json",
    "authority-membership.json",
)

_READ_ERRORS = (
    FileNotFoundError,
    KeyError,
    OSError,
    TypeError,
    ValueError,
    json.JSONDecodeError,
)


def _trusted_regular(path: Path, *, mode: int, expected_uid: int) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != expected_uid
        or stat.S_IMODE(info.st_mode) != mode
        or info.st_nlink != 1
    ):
        raise RuntimeError(f"untrusted recovery file: {path}")
    payload = path.read_bytes()
    final = path.lstat()
    if (
        final.st_dev != info.st_dev
        or final.st_ino != info.st_ino
        or final.st_size != info.st_size
        or final.st_mtime_ns != info.st_mtime_ns
        or final.st_ctime_ns != info.st_ctime_ns
    ):
        raise RuntimeError(f"recovery file changed while being read: {path}")
    return payload


def _install_capsule(
    state_dir: Path, *, expected_uid: int
) -> tuple[Path, dict[str, object]]:
    pending = state_dir / "pending.json"
    payload = json.loads(
        _trusted_regular(pending, mode=0o600, expected_uid=expected_uid)
    )
    if not isinstance(payload, dict):
        raise RuntimeError("install recovery journal is invalid")
    recovery_path = Path(payload["recovery"])
    expected_prefix = re.escape(str(state_dir.resolve()))
    pattern = re.compile(
        rf"{expected_prefix}/generation-[a-f0-9]{{16}}/recovery\.py"
    )
    recovery = str(recovery_path.resolve(strict=True))
    if str(recovery_path) != recovery or not pattern.fullmatch(recovery):
        raise RuntimeError("install recovery capsule path is invalid")
    _trusted_regular(recovery_path, mode=0o700, expected_uid=expected_uid)
    return recovery_path, payload


def _uninstall_capsule(state_dir: Path, *, expected_uid: int) -> Path:
    journal = state_dir / "uninstall.json"
    payload = json.loads(
        _trusted_regular(journal, mode=0o600, expected_uid=expected_uid)
    )
    required = {
        "version",
        "transaction_id",
        "phase",
        "baseline_selector",
        "start_selector",
        "registry_sha256",
        "snapshot_sha256",
        "recovery",
        "recovery_sha256",
    }
    if (
        set(payload) != required
        or payload["version"] != 2
        or payload["phase"] not in {"INTENT", "ARMED", "COMPLETING"}
        or not isinstance(payload["transaction_id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", payload["transaction_id"])
        or not isinstance(payload["recovery"], str)
        or not isinstance(payload["recovery_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["recovery_sha256"])
        or not isinstance(payload["baseline_selector"], str)
        or (
            payload["baseline_selector"] != "ABSENT"
            and not re.fullmatch(
                r"releases/[0-9a-f]{40}", payload["baseline_selector"]
            )
        )
        or not isinstance(payload["start_selector"], str)
        or (
            payload["start_selector"] != "ABSENT"
            and not re.fullmatch(
                r"releases/[0-9a-f]{40}", payload["start_selector"]
            )
        )
        or not isinstance(payload["registry_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["registry_sha256"])
        or (
            payload["phase"] == "INTENT"
            and payload["snapshot_sha256"] is not None
        )
        or (
            payload["phase"] != "INTENT"
            and (
                not isinstance(payload["snapshot_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["snapshot_sha256"])
            )
        )
    ):
        raise RuntimeError("uninstall recovery journal is invalid")
    recovery_path = Path(payload["recovery"])
    expected = (
        state_dir.resolve()
        / f"uninstall-{payload['transaction_id']}"
        / "recovery.py"
    )
    recovery = recovery_path.resolve(strict=True)
    if recovery_path != recovery or recovery != expected:
        raise RuntimeError("uninstall recovery capsule path is invalid")
    capsule = _trusted_regular(recovery, mode=0o700, expected_uid=expected_uid)
    if hashlib.sha256(capsule).hexdigest() != payload["recovery_sha256"]:
        raise RuntimeError("uninstall recovery capsule digest drifted")
    return recovery


def _auxiliary_capsule(state_dir: Path, *, expected_uid: int) -> Path:
    """Resolve legacy auxiliary-only WAL to its exact live capsule."""
    current = json.loads(
        _trusted_regular(
            state_dir / "current.json", mode=0o600, expected_uid=expected_uid
        )
    )
    if not isinstance(current, dict) or not isinstance(current.get("manifest"), str):
        raise RuntimeError("current generation pointer is invalid")
    manifests: set[Path] = set()
    for name in AUXILIARY_JOURNALS:
        journal = state_dir / name
        try:
            payload = json.loads(
                _trusted_regular(journal, mode=0o600, expected_uid=expected_uid)
            )
        except FileNotFoundError:
            continue
        if not isinstance(payload, dict):
            raise RuntimeError("auxiliary recovery journal is invalid")
        manifest_value = payload.get("manifest")
        if name == "sensitive-retirement.json" and payload.get("version") == 1:
            generation = payload.get("generation")
            if not isinstance(generation, str):
                raise RuntimeError("legacy retirement generation is invalid")
            manifest_value = str(state_dir / generation / "manifest.json")
        if not isinstance(manifest_value, str):
            raise RuntimeError("auxiliary recovery journal has no manifest")
        manifests.add(Path(manifest_value))
    if len(manifests) != 1:
        raise RuntimeError("auxiliary recovery journals disagree on generation")
    manifest = manifests.pop()
    generation = manifest.parent
    state_root = state_dir.resolve(strict=True)
    expected_generation = state_root / generation.name
    expected = expected_generation / "manifest.json"
    if manifest != expected or not re.fullmatch(
        r"generation-[0-9a-f]{16}", generation.name
    ):
        raise RuntimeError("auxiliary recovery generation is invalid")
    if current["manifest"] != str(manifest):
        # A journal naming a non-live generation is not corruption: it is
        # the durable record of a rolled-back control transition, and the
        # generation it names may already be deleted as an orphan. The
        # capsule that can resolve it is the LIVE generation's -- its
        # `recover()` decides the journal's direction from `current.json`,
        # restores the memberships the rollback owes, and consumes the
        # journal. Refusing here left exactly that host with no boot
        # recovery at all (acceptance finding on 0e856b9a). The fallback
        # capsule passes every trust check below that the journal's own
        # would have.
        manifest = Path(current["manifest"])
        generation = manifest.parent
        expected_generation = state_root / generation.name
        expected = expected_generation / "manifest.json"
        if manifest != expected or not re.fullmatch(
            r"generation-[0-9a-f]{16}", generation.name
        ):
            raise RuntimeError("live generation pointer is invalid")
    generation_info = expected_generation.lstat()
    if (
        not stat.S_ISDIR(generation_info.st_mode)
        or stat.S_ISLNK(generation_info.st_mode)
        or generation_info.st_uid != expected_uid
        or stat.S_IMODE(generation_info.st_mode) != 0o700
        or expected_generation.resolve(strict=True) != expected_generation
    ):
        raise RuntimeError("auxiliary recovery generation directory is untrusted")
    if manifest.resolve(strict=True) != expected:
        raise RuntimeError("auxiliary recovery manifest escaped its generation")
    _trusted_regular(expected, mode=0o600, expected_uid=expected_uid)
    recovery = expected_generation / "recovery.py"
    if recovery.resolve(strict=True) != recovery:
        raise RuntimeError("auxiliary recovery capsule escaped its generation")
    _trusted_regular(recovery, mode=0o700, expected_uid=expected_uid)
    return recovery


def recover(state_dir: Path = STATE_DIR, *, expected_uid: int = 0) -> int:
    """Dispatch a boot recovery capsule after local filesystems are mounted."""
    def present(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    pending = present(state_dir / "pending.json")
    pending_release = present(state_dir / "pending-release")
    uninstall = present(state_dir / "uninstall.json")
    auxiliary = any(present(state_dir / name) for name in AUXILIARY_JOURNALS)
    if pending_release and not pending:
        raise RuntimeError("pending release selector exists without install journal")
    if pending and uninstall:
        raise RuntimeError("install and uninstall recovery journals coexist")
    if uninstall and auxiliary:
        raise RuntimeError("uninstall and auxiliary recovery journals coexist")
    if not pending and not uninstall and not auxiliary:
        return 0
    try:
        if uninstall:
            capsule = _uninstall_capsule(state_dir, expected_uid=expected_uid)
            action = "recover-uninstall-boot"
        elif auxiliary and not pending:
            capsule = _auxiliary_capsule(state_dir, expected_uid=expected_uid)
            action = "recover-boot"
        else:
            # pending-release without pending.json is invalid: only the
            # generation capsule recorded by pending.json is trusted code.
            capsule, install_journal = _install_capsule(
                state_dir, expected_uid=expected_uid
            )
            if pending_release and install_journal.get("phase") not in {
                "APPLIED",
                "COMMITTING",
            }:
                raise RuntimeError(
                    "pending release selector is not paired with an applied install"
                )
            action = "recover-boot"
    except _READ_ERRORS as exc:
        raise RuntimeError("principal recovery journal is malformed") from exc
    os.execv(
        "/usr/bin/python3",
        [
            "/usr/bin/python3",
            str(capsule),
            action,
            "--state-dir",
            str(state_dir),
        ],
    )
    raise AssertionError("unreachable")


def _boot_id() -> str:
    """Per-boot random id; empty when unavailable so callers fail closed."""
    try:
        return (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        return ""


def _systemctl(run, *arguments: str) -> tuple[int, str]:
    result = run(
        ["/usr/bin/systemctl", *arguments],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.returncode, (result.stdout or "").strip()


def complete_deferred_starts(
    state_dir: Path = STATE_DIR, *, expected_uid: int = 0, run=subprocess.run
) -> int:
    """Prove the starts boot recovery could only enqueue actually happened.

    Boot recovery restores files and unit enablement durably, but it runs
    before sysinit.target and can only `--no-block start` the units the
    snapshot recorded active -- it cannot wait for them without deadlocking on
    its own ordering. It then consumes the WAL. Without this step a queued job
    that fails afterwards leaves a previously active lane inactive and no
    evidence that anything is owed (independent review on 988de49).

    The journal is consumed only on proof, so a failure here keeps naming the
    units that still owe an active state, and this unit stays `failed` where
    an operator and the fleet's own health probes can see it.
    """
    journal = state_dir / DEFERRED_STARTS
    try:
        raw = _trusted_regular(journal, mode=0o600, expected_uid=expected_uid)
    except FileNotFoundError:
        return 0
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("deferred-start journal is malformed") from exc
    units = payload.get("units") if isinstance(payload, dict) else None
    recorded_boot = payload.get("boot_id") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != DEFERRED_STARTS_VERSION
        or not isinstance(units, list)
        or not units
        or not all(
            isinstance(unit, str) and DEFERRED_UNIT_RE.fullmatch(unit)
            for unit in units
        )
        or not isinstance(recorded_boot, str)
    ):
        raise RuntimeError("deferred-start journal is invalid")
    current_boot = _boot_id()
    # An empty id on either side means the binding cannot be evaluated at all.
    # Refuse rather than guess: the journal stays, and so does the evidence.
    if not current_boot or not recorded_boot:
        raise RuntimeError("cannot bind deferred starts to a boot")
    same_boot = recorded_boot == current_boot
    unproven: list[str] = []
    for unit in units:
        if same_boot:
            # Synchronous: when a start job for this unit is already queued,
            # systemctl joins that job and reports ITS result, which is
            # exactly the outcome the deferral left unproven.
            _systemctl(run, "start", unit)
        _active_rc, active = _systemctl(run, "is-active", unit)
        if active != "active":
            unproven.append(unit)
    if unproven:
        raise RuntimeError(
            "deferred starts did not reach active: " + ", ".join(sorted(unproven))
        )
    journal.unlink(missing_ok=True)
    descriptor = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return 0


def _atomic_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}"
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)


def generate(early_dir: Path, state_dir: Path = STATE_DIR) -> bool:
    """Always emit the early recovery unit and admission dependencies."""
    unit = early_dir / RECOVERY_UNIT
    _atomic_text(
        unit,
        "[Unit]\n"
        "Description=Fail-closed AICC principal-isolation recovery barrier\n"
        "DefaultDependencies=no\n"
        f"RequiresMountsFor={state_dir} /opt/aicc\n"
        "After=local-fs.target\n"
        "Before=sysinit.target basic.target shutdown.target\n"
        "Conflicts=shutdown.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart={ANCHOR} --recover {state_dir}\n"
        "NoNewPrivileges=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=yes\n"
        "PrivateTmp=yes\n"
        "ReadWritePaths=-/opt/aicc -/etc/aicc /etc/systemd/system "
        "/usr/lib/systemd/system-generators /usr/lib/sysusers.d "
        "/usr/lib/tmpfiles.d /usr/libexec -/var/lib/aicc-agent "
        "/var/lib/aicc-principal-isolation\n",
    )
    # The completion half. Emitted unconditionally, exactly like the barrier
    # above: whether anything was deferred is not knowable while generators
    # run, and a no-op run costs one journal lstat. Ordered after the boot has
    # settled so the queued jobs have had their chance, and pulled in by
    # `Wants=` rather than `Requires=` -- an unproven start must fail THIS
    # unit loudly, not take multi-user.target down with it.
    _atomic_text(
        early_dir / COMPLETION_UNIT,
        "[Unit]\n"
        "Description=Prove AICC principal-isolation recovery finished its "
        "deferred starts\n"
        f"RequiresMountsFor={state_dir}\n"
        "After=multi-user.target\n"
        "Conflicts=shutdown.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        # Bounded: joining a queued job means waiting for it, and a job that
        # never completes must surface as a failed completion (which keeps the
        # journal) rather than as a unit that waits forever.
        "TimeoutStartSec=15min\n"
        f"ExecStart={ANCHOR} --complete-deferred-starts {state_dir}\n"
        "NoNewPrivileges=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=yes\n"
        "PrivateTmp=yes\n"
        f"ReadWritePaths={state_dir}\n",
    )
    wants = early_dir / "multi-user.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    completion = wants / COMPLETION_UNIT
    try:
        completion.symlink_to(f"../{COMPLETION_UNIT}")
    except FileExistsError:
        if (
            not completion.is_symlink()
            or os.readlink(completion) != f"../{COMPLETION_UNIT}"
        ):
            raise RuntimeError("recovery completion pull-in was pre-populated")
    requires = early_dir / "sysinit.target.requires"
    requires.mkdir(parents=True, exist_ok=True)
    dependency = requires / RECOVERY_UNIT
    try:
        dependency.symlink_to(f"../{RECOVERY_UNIT}")
    except FileExistsError:
        if (
            not dependency.is_symlink()
            or os.readlink(dependency) != f"../{RECOVERY_UNIT}"
        ):
            raise RuntimeError("recovery pull-in dependency was pre-populated")
    dropin = (
        "[Unit]\n"
        f"Requires={RECOVERY_UNIT}\n"
        f"After={RECOVERY_UNIT}\n"
    )
    for claimer in CLAIMERS:
        _atomic_text(early_dir / f"{claimer}.d/10-aicc-recovery.conf", dropin)
    return True


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--recover":
        state_dir = Path(sys.argv[2]) if len(sys.argv) == 3 else STATE_DIR
        return recover(state_dir)
    if len(sys.argv) >= 2 and sys.argv[1] == "--complete-deferred-starts":
        state_dir = Path(sys.argv[2]) if len(sys.argv) == 3 else STATE_DIR
        return complete_deferred_starts(state_dir)
    # systemd passes normal-dir, early-dir and late-dir. early-dir has higher
    # precedence than /etc, so an obsolete unit or mask cannot bypass recovery.
    if len(sys.argv) != 4:
        return 1
    generate(Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
