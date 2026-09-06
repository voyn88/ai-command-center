"""The publish path that keeps the control plane's store current.

Nothing ever ran `backlog-import` on a schedule, and the consequence was
measured live on 2026-08-31: a control plane deciding from a snapshot five
days stale, 32 tasks it had never seen, and a fully green pull request no tick
could pick up because the task behind it still read `OPEN`.

Every test here drives a fake runner. The point is the sequence and its
refusals -- copy, verify on the host, import, always remove -- not ssh.
"""

from __future__ import annotations

import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _module():
    path = ROOT / "ops" / "aicc_backlog_publish.py"
    spec = importlib.util.spec_from_file_location("aicc_backlog_publish", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _backlog(tmp_path, text="- **VOYN-W0-X** | Wave 0 | OPEN | P0 | d | `s` | body\n"):
    path = tmp_path / "VOYN_TASKS_BACKLOG.md"
    path.write_text(text, encoding="utf-8")
    return path


def _runner(digest_out=None, *, import_rc=0, import_out="inserted 1, updated 2, unchanged 3"):
    """Records every command, answers like a healthy host unless told otherwise."""
    calls: list[list[str]] = []

    def run(argv, *, stdin_path=None):
        calls.append(argv)
        joined = " ".join(argv)
        if argv[0] == "scp":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "sha256sum" in joined:
            return subprocess.CompletedProcess(argv, 0, (digest_out or "") + "\n", "")
        if "backlog-import" in joined:
            return subprocess.CompletedProcess(argv, import_rc, import_out, "boom")
        return subprocess.CompletedProcess(argv, 0, "", "")

    run.calls = calls
    return run


def _publish(module, backlog, runner):
    return module.publish(
        backlog, host="root@h", repo="/repo", env_file="/env", runner=runner
    )


def test_a_healthy_publish_copies_verifies_imports_and_cleans_up(tmp_path):
    module = _module()
    backlog = _backlog(tmp_path)
    runner = _runner(module.digest_of(backlog))

    report = _publish(module, backlog, runner)

    assert report == "inserted 1, updated 2, unchanged 3"
    stages = [" ".join(argv) for argv in runner.calls]
    assert stages[0].startswith("scp")
    assert "sha256sum" in stages[1]
    assert "backlog-import" in stages[2]
    assert stages[3].startswith("ssh") and " rm -f " in stages[3]


def test_a_digest_mismatch_on_the_host_refuses_before_importing(tmp_path):
    """A transfer that lost or altered bytes must never reach the store."""
    module = _module()
    backlog = _backlog(tmp_path)
    runner = _runner("f" * 64)

    with pytest.raises(module.PublishError, match="digest mismatch"):
        _publish(module, backlog, runner)

    assert not any("backlog-import" in " ".join(argv) for argv in runner.calls)


def test_the_staging_copy_is_removed_even_when_the_import_fails(tmp_path):
    """The copy exists only for one publish. Leaving it behind on failure
    would create the very thing this avoids: a second file something could
    later import from."""
    module = _module()
    backlog = _backlog(tmp_path)
    runner = _runner(module.digest_of(backlog), import_rc=1)

    with pytest.raises(module.PublishError, match="import failed"):
        _publish(module, backlog, runner)

    assert any(" rm -f " in " ".join(argv) for argv in runner.calls)


def test_an_import_that_reports_nothing_is_a_failure(tmp_path):
    """`inserted …` is the only proof the store actually changed hands. A
    silent success would let a broken import look like a current store."""
    module = _module()
    backlog = _backlog(tmp_path)
    runner = _runner(module.digest_of(backlog), import_out="")

    with pytest.raises(module.PublishError, match="no report line"):
        _publish(module, backlog, runner)


def test_a_missing_or_empty_backlog_is_refused_before_any_remote_call(tmp_path):
    module = _module()
    runner = _runner("")

    with pytest.raises(module.PublishError, match="not a file"):
        _publish(module, tmp_path / "absent.md", runner)

    empty = _backlog(tmp_path, text="")
    with pytest.raises(module.PublishError, match="empty"):
        _publish(module, empty, runner)

    assert runner.calls == []


def test_the_import_runs_as_the_unprivileged_control_plane_account(tmp_path):
    """Root has the ssh key; the import must not inherit it. It runs as the
    account the control plane's own units use, whose database role is the one
    the store expects."""
    module = _module()
    backlog = _backlog(tmp_path)
    runner = _runner(module.digest_of(backlog))

    _publish(module, backlog, runner)

    imported = next(a for a in runner.calls if "backlog-import" in " ".join(a))
    assert f"su - {module.IMPORT_USER} -c" in " ".join(imported)


def test_the_launchd_agent_reconciles_on_an_interval_rather_than_respawning():
    agent = plistlib.loads(
        (ROOT / "deploy" / "com.ai-command-center.backlog-publish.plist").read_bytes()
    )

    assert agent["ProgramArguments"][1].endswith("ops/aicc_backlog_publish.py")
    # One tick of the control plane: the store is never more than one tick
    # behind the file.
    assert agent["StartInterval"] == 300
    # A failing publish must not spin: the next interval is soon enough.
    assert "KeepAlive" not in agent
