"""A deferred deploy must not hide failed timer restoration."""
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="rollout requires pwd/fcntl")
def test_strict_timer_restore_attempts_remaining_timers_after_failure(monkeypatch):
    path = Path(__file__).parents[2] / "ops/aicc_staged_worker_rollout.py"
    spec = importlib.util.spec_from_file_location("timer_restore_rollout", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LANE_MUTATING_TIMERS", ("broken.timer", "absent.timer", "good.timer"))
    started = []

    class Systemd:
        def run(self, *args, **kwargs):
            if args[0] == "show":
                return "not-found" if args[1] == "absent.timer" else "loaded"
            started.append(args[1])
            if args[1] == "broken.timer":
                raise module.RolloutError("start failed")
            return ""

    with pytest.raises(module.RolloutError, match="timer_restore_failed:broken.timer"):
        module.release_lane_mutating_timers(Systemd(), strict=True)
    assert started == ["broken.timer", "good.timer"]
