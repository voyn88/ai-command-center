"""One lock, one meaning, across six programs that cannot import each other.

The staged-rollout lock is spelled out in a Python package module, a
standalone ops script run by /usr/bin/python3, a POSIX shell installer, and
four systemd units. Nothing in the language makes those agree, and the
original change said so in a comment -- "keep the three in sync by hand" --
which is not a mechanism: if two of them ever name different files, or
disagree about what "held" means, every consumer simply finds no lock and
proceeds, and the failure mode is silence. The list drifted in exactly that
way once already (voyn-aicc-source-clone-refresh.timer joined
LANE_MUTATING_TIMERS in #917 while the installer went on holding two of the
three).

So this file is the mechanism. It asserts the copies carry the same path and
the same timers, and -- because agreeing on a filename is worth little if
the readers disagree on what its presence means -- it RUNS all three
implementations of "is it held" against the same scenarios: the package
function, the ops script's copy, and the `/bin/sh` command the systemd units
actually execute in ExecCondition.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from command_center.deployment.self_deploy import SelfDeployConfig
from command_center.ops.credential_rotation import DEFAULT_ROLLOUT_LOCK_PATH
from command_center.ops.rollout_lock import ROLLOUT_LOCK_PATH, rollout_lock_is_held

REPO = Path(__file__).parents[2]
INSTALLER = REPO / "deploy/install-agent-principal-isolation.sh"
UNITS = REPO / "deploy/systemd"

#: Every unit whose ExecCondition has to honour the lock: the service behind
#: each lane-mutating timer, plus the worker variant of the self-deploy
#: service (installed AS voyn-aicc-self-deploy.service on worker hosts,
#: which is where the rollout runs at all).
CONDITIONED_UNITS = (
    "voyn-aicc-credential-rotation.service",
    "voyn-aicc-self-deploy.service",
    "voyn-aicc-self-deploy-worker.service",
    "voyn-aicc-source-clone-refresh.service",
)


def _rollout_module():
    """ops/aicc_staged_worker_rollout.py, loaded unpatched.

    Deliberately NOT the heavily seamed loader in
    tests/ops/test_aicc_staged_worker_rollout.py: that one rebinds
    ROLLOUT_LOCK_PATH to a writable temp directory, which is right for
    exercising the write path and useless for asking what the shipped
    constant says.
    """
    path = REPO / "ops/aicc_staged_worker_rollout.py"
    spec = importlib.util.spec_from_file_location("aicc_staged_worker_rollout", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _installer_text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _shell_assignment(name: str) -> str:
    match = re.search(rf'^{name}="?([^"\n]+)"?$', _installer_text(), re.MULTILINE)
    assert match, f"{name}= not found in {INSTALLER.name}"
    return match.group(1)


def _exec_conditions() -> dict[str, str]:
    found: dict[str, str] = {}
    for name in CONDITIONED_UNITS:
        for line in (UNITS / name).read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecCondition="):
                assert name not in found, f"{name} has more than one ExecCondition"
                found[name] = line.split("=", 1)[1]
    return found


def test_every_copy_of_the_lock_path_is_the_same_path():
    """Six independent spellings, one file. A single divergence would not
    fail anything at runtime -- it would just quietly disarm the lock for
    whichever program held the odd one out."""
    expected = str(ROLLOUT_LOCK_PATH)

    assert str(DEFAULT_ROLLOUT_LOCK_PATH) == expected
    assert SelfDeployConfig().rollout_lock_path == expected
    assert _rollout_module().ROLLOUT_LOCK_PATH == ROLLOUT_LOCK_PATH
    assert _shell_assignment("rollout_lock") == expected

    for name, condition in _exec_conditions().items():
        assert expected in condition, name

    rotation = (UNITS / "voyn-aicc-credential-rotation.service").read_text(
        encoding="utf-8"
    )
    # Both invocations -- the rotation itself and the ExecStopPost recovery
    # -- are pointed at the same marker.
    assert rotation.count(f"--rollout-lock {expected}") == 2


def test_no_module_inside_the_package_re_spells_the_path():
    """Inside `command_center` the path is importable, so a literal is never
    the right answer there -- it is a copy nothing holds to account. (The
    copies OUTSIDE the package are unavoidable, which is what the rest of
    this file is for.)"""
    literal = str(ROLLOUT_LOCK_PATH)
    source = REPO / "command_center/ops/rollout_lock.py"
    offenders = [
        path.relative_to(REPO)
        for path in (REPO / "command_center").rglob("*.py")
        if path != source and literal in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_the_installer_holds_exactly_the_timers_the_rollout_declares():
    """The drift that actually happened: a third timer joined the Python
    tuple and this transaction went on stopping two of them, so a
    source-clone refresh could still move the tree under a rollout that
    believed it had the host to itself."""
    declared = _rollout_module().LANE_MUTATING_TIMERS
    held = tuple(_shell_assignment("lane_mutating_timers").split())

    assert held == declared


def test_every_lane_mutating_timer_backs_its_service_with_the_condition():
    """Stopping the timer is the first defense; the condition is what
    catches the tick that was already queued when it stopped. A timer held
    by the rollout whose service has no condition is half a guardrail."""
    conditions = _exec_conditions()
    declared = _rollout_module().LANE_MUTATING_TIMERS

    for timer in declared:
        unit = (UNITS / timer).read_text(encoding="utf-8")
        # systemd defaults an omitted Unit= to the timer's own basename with
        # a .service suffix.
        target = next(
            (
                line.split("=", 1)[1]
                for line in unit.splitlines()
                if line.startswith("Unit=")
            ),
            timer.removesuffix(".timer") + ".service",
        )
        assert target in conditions, f"{target} (behind {timer}) has no ExecCondition"

    # All of them run the SAME condition -- one meaning of "held", not four.
    assert len(set(conditions.values())) == 1


# ---------------------------------------------------------------------------
# The three implementations of "is it held", driven against one scenario set.
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def _scenarios(tmp_path: Path) -> list[tuple[str, Path, bool]]:
    absent = tmp_path / "absent.lock"

    def written(name: str, contents: str) -> Path:
        path = tmp_path / name
        path.write_text(contents, encoding="utf-8")
        return path

    return [
        ("absent", absent, False),
        ("crashed holder", written("dead.lock", f"{_dead_pid()}\n"), False),
        ("live holder", written("live.lock", f"{os.getpid()}\n"), True),
        ("empty", written("empty.lock", ""), True),
        ("blank", written("blank.lock", "   \n"), True),
        ("not a pid", written("junk.lock", "rollout\n"), True),
        ("negative", written("negative.lock", "-1\n"), True),
        ("zero", written("zero.lock", "0\n"), True),
        ("leading zero", written("padded.lock", "0123\n"), True),
    ]


def _exec_condition_says_held(condition: str, lock: Path) -> bool:
    """Run the units' ExecCondition for real.

    systemd unescapes `$$` to a literal `$` and splits the single-quoted
    argument into one word before exec'ing it; this reproduces both steps so
    the thing under test is the command the host actually runs, not a
    paraphrase of it.
    """
    prefix = "/bin/sh -c "
    assert condition.startswith(prefix), condition
    script = condition[len(prefix) :]
    assert script.startswith("'") and script.endswith("'"), script
    script = script[1:-1].replace("$$", "$")
    script = script.replace(str(ROLLOUT_LOCK_PATH), str(lock))
    result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)
    assert result.returncode in (0, 1), result
    # Exit 0 continues to ExecStart (not held); 1-254 skips the unit (held).
    return result.returncode == 1


@pytest.mark.parametrize(
    "reader",
    ["package", "rollout script", "systemd ExecCondition"],
)
def test_all_three_readers_agree_on_what_held_means(tmp_path, reader, monkeypatch):
    """A lock the rotator treats as stale and the unit treats as held (or
    the reverse) is worse than no lock: the halves of the defense would
    disagree exactly when it matters, and nothing would say so."""
    module = _rollout_module()
    condition = next(iter(_exec_conditions().values()))

    def held(lock: Path) -> bool:
        if reader == "package":
            return rollout_lock_is_held(lock)
        if reader == "rollout script":
            monkeypatch.setattr(module, "ROLLOUT_LOCK_PATH", lock)
            return module.rollout_lock_is_held()
        return _exec_condition_says_held(condition, lock)

    for name, lock, expected in _scenarios(tmp_path):
        assert held(lock) is expected, f"{reader} disagreed on: {name}"
