"""Exercise installer locking and timer retirement without touching systemd."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="requires bash")


def _run(tmp_path, commands, active_state="inactive"):
    source = (Path(__file__).parents[2] / "deploy/install-agent-principal-isolation.sh").read_text()
    functions = "\n".join(
        re.search(rf"^{name}\(\) \{{.*?^\}}", source, re.MULTILINE | re.DOTALL).group()
        for name in ("hold_lane_timers", "release_lane_timers")
    ).replace("/run/aicc-staged-rollout.lock", str(tmp_path / "lock"))
    stub = '''
systemctl() {
  printf '%s\\n' "$*" >> "$CALL_LOG"
  case "$1" in
    is-enabled) [ "$3" != voyn-aicc-self-deploy.timer ] ;;
    show) if [ "$3" = --property=ActiveState ]; then
            printf '%s\\n' "$DEPLOY_STATE"; return 0
          fi
          printf 'loaded\\n' ;;
    *) return 0 ;;
  esac
}
timers_held=0
'''
    return subprocess.run(["bash", "-c", stub + functions + "\n" + commands],
                          env={"PATH": "/usr/bin:/bin", "CALL_LOG": str(tmp_path / "calls"), "DEPLOY_STATE": active_state},
                          capture_output=True, text=True, check=False)


def test_installer_refuses_foreign_lock_without_touching_timers(tmp_path):
    (tmp_path / "lock").write_text("foreign-owner\n")
    result = _run(tmp_path, "hold_lane_timers")
    assert result.returncode != 0
    assert (tmp_path / "lock").read_text() == "foreign-owner\n"
    assert not (tmp_path / "calls").exists()


def test_installer_releases_only_enabled_timers(tmp_path):
    result = _run(tmp_path, "hold_lane_timers && release_lane_timers")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert "start voyn-aicc-worker-release-deploy.timer" in calls
    assert "start voyn-aicc-credential-rotation.timer" in calls
    assert "start voyn-aicc-self-deploy.timer" not in calls
    assert not (tmp_path / "lock").exists()


def test_installer_release_preserves_foreign_lock(tmp_path):
    (tmp_path / "lock").write_text("foreign-owner\n")
    result = _run(tmp_path, "timers_held=1; release_lane_timers")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "lock").read_text() == "foreign-owner\n"


def test_deploy_unit_cannot_access_production_checkout():
    unit = (Path(__file__).parents[2] / "deploy/systemd/voyn-aicc-worker-release-deploy.service").read_text()
    assert "InaccessiblePaths=-/opt/aicc/source" in unit


@pytest.mark.parametrize("active_state", ["active", "activating", "deactivating"])
def test_installer_refuses_live_deploy_before_timer_changes(tmp_path, active_state):
    result = _run(tmp_path, "hold_lane_timers; result=$?; release_lane_timers; exit $result", active_state)
    assert result.returncode != 0
    calls = (tmp_path / "calls").read_text().splitlines()
    assert not any(call.startswith("stop ") for call in calls)
    assert not (tmp_path / "lock").exists()
