"""Isolates every test in this session from the developer's real `data/` directory.

`AICC_DATA_DIR` (see `command_center.storage.resolve_data_dir`) is set here, at
conftest import time — before pytest imports any test module or `app.py` — so every
`command_center` module's file-level `DATA_DIR`/`*_FILE` constants resolve into a
throwaway temp directory instead of the developer's real `data/tasks.json`,
`data/runs.jsonl`, etc. Those constants are computed once, at first import, so all
tests in a session share the same temp directory; `isolated_data_dir` (autouse) resets
its *contents* between tests rather than re-pointing it.
"""

from __future__ import annotations

import os
import shutil
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def restore_main_module_after_streamlit_apptest():
    """Keep AppTest's temporary ``__main__`` from poisoning spawn workers.

    Streamlit replaces ``sys.modules["__main__"]`` on each run.  A later
    multiprocessing test using the spawn start method otherwise re-executes
    Streamlit's deleted temporary script instead of pytest's real entrypoint.
    """
    original = sys.modules.get("__main__")
    yield
    if original is None:
        sys.modules.pop("__main__", None)
    else:
        sys.modules["__main__"] = original

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="aicc_test_data_"))
os.environ["AICC_DATA_DIR"] = str(_TEST_DATA_DIR)


@pytest.fixture(autouse=True)
def clear_provider_probe_cache():
    """Provider availability is memoized for a short TTL (see
    `runtime.providers._PROBE_CACHE_TTL_SECONDS`). Tests install and remove fake
    provider binaries between cases, so a verdict cached by one test must never
    be read by the next."""
    from command_center.runtime import providers

    providers.clear_probe_cache()
    yield
    providers.clear_probe_cache()


@pytest.fixture(autouse=True)
def _immediate_reconcile(monkeypatch):
    """reconcile()'s cross-process debounce (audit P0) waits a grace window before
    terminalizing a run that only *looks* gone. Tests want deterministic,
    immediate classification, so default the grace to 0 across the suite; the
    debounce behaviour itself is covered in test_runtime_reconciliation.py, which
    overrides `sup._reconcile_absence_grace` locally."""
    from command_center.runtime import supervisor

    monkeypatch.setattr(supervisor, "_RECONCILE_ABSENCE_GRACE_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def isolated_data_dir():
    if _TEST_DATA_DIR.exists():
        shutil.rmtree(_TEST_DATA_DIR)
    _TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _clear_execution_center_singleton_cache()
    yield _TEST_DATA_DIR
    if _TEST_DATA_DIR.exists():
        shutil.rmtree(_TEST_DATA_DIR)


def _clear_execution_center_singleton_cache() -> None:
    """`app.py`'s `get_execution_center_api()` is `@st.cache_resource` —
    cached process-wide, not per-`AppTest`-instance. Since the Live
    Execution Center v2 bridge means *any* Kanban task launch (not just a
    visit to the Live Execution Center page) now constructs that singleton,
    every test that resets `AICC_DATA_DIR` must also clear this cache —
    otherwise a `Supervisor` cached from a previous test would keep pointing
    at a `runtime.db` path this fixture just deleted and recreated fresh
    (unmigrated), surfacing as `sqlite3.OperationalError: no such table:
    run`. Streamlit is imported lazily here so modules that never touch
    Streamlit at all aren't forced to import it just for this cleanup —
    and guarded, because a headless worker host installs no desktop extras
    at all: the SRV-05 daemon tests run on machines where streamlit is
    deliberately absent, and an autouse fixture that hard-imports it turns
    every such run red at setup for a dependency the tests under test
    never touch."""
    try:
        import streamlit as st
    except ImportError:
        return
    st.cache_resource.clear()


@pytest.fixture(autouse=True)
def isolated_reports_dir(isolated_data_dir, monkeypatch):
    """`agent_runner.REPORTS_ROOT` is a module-level constant derived from `ROOT`, not
    `AICC_DATA_DIR` (reports live at `<repo>/reports/`, not under `data/`). Any test
    that exercises a full launch flow — including via Streamlit `AppTest`, which runs
    the real `app.py` — would otherwise write real files into the developer's actual
    `reports/<PROJECT>/` directory. Applied to every test automatically so nobody has
    to remember it (a real leak into `reports/AIOS/` from this exact gap was caught
    and cleaned up manually before this fixture existed)."""
    from command_center import agent_runner
    from command_center.runtime import reports as runtime_reports

    reports_dir = isolated_data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_runner, "REPORTS_ROOT", reports_dir)
    monkeypatch.setattr(runtime_reports, "REPORTS_ROOT", reports_dir)


@pytest.fixture(autouse=True)
def isolated_module_data_constants(isolated_data_dir, monkeypatch):
    """Defense-in-depth for the same class of gap `isolated_reports_dir` closes for
    `REPORTS_ROOT`, applied to every module that computes `DATA_DIR`/`*_FILE` once at
    first import (`project_config.py`, `agent_runner.py`, `activity_log.py`,
    `chat_service.py` — see each module's `DATA_DIR = storage.resolve_data_dir(ROOT)`
    line). Setting `AICC_DATA_DIR` before pytest imports any test module (see the
    module docstring above) is sufficient for a normal `pytest` invocation, but it is
    not the only way these modules can end up imported: a persistent Python process
    (a REPL, a notebook, an agent running a one-off repro snippet) that imports one of
    them *before* `AICC_DATA_DIR` is set freezes `DATA_DIR` to the real `data/`
    directory for the rest of that process, and a subsequent
    `save_repository_path`/`log_event`/etc. call silently writes real files instead of
    raising — this is exactly how a pytest `tmp_path` (e.g.
    `.../pytest-184/test_full_launch_flow_records_0/aios-fake-repo`) ended up
    persisted into the developer's real `data/project_config.json` `AIOS.repository_path`
    (found and fixed as part of the runtime-integrity incident this fixture responds
    to). Directly monkeypatching each module's constants — rather than relying solely
    on import ordering — makes isolation hold regardless of when/how each module was
    first imported into the test process."""
    from command_center import activity_log, agent_runner, chat_service, portfolio_config, project_config

    monkeypatch.setattr(project_config, "DATA_DIR", isolated_data_dir)
    monkeypatch.setattr(project_config, "CONFIG_FILE", isolated_data_dir / "project_config.json")
    monkeypatch.setattr(agent_runner, "DATA_DIR", isolated_data_dir)
    monkeypatch.setattr(agent_runner, "RUNS_FILE", isolated_data_dir / "runs.jsonl")
    monkeypatch.setattr(activity_log, "DATA_DIR", isolated_data_dir)
    monkeypatch.setattr(activity_log, "ACTIVITY_FILE", isolated_data_dir / "activity.jsonl")
    monkeypatch.setattr(chat_service, "DATA_DIR", isolated_data_dir)
    monkeypatch.setattr(chat_service, "CHATS_FILE", isolated_data_dir / "chats.json")
    monkeypatch.setattr(portfolio_config, "DATA_DIR", isolated_data_dir)
    monkeypatch.setattr(portfolio_config, "CONFIG_FILE", isolated_data_dir / "portfolio_config.json")


@pytest.fixture(autouse=True)
def isolated_generated_dir(isolated_data_dir, monkeypatch):
    """Closes the same class of gap `isolated_reports_dir` closes for `REPORTS_ROOT`,
    for `app.GENERATED_DIR` / `command_center.workspace_home.GENERATED_DIR` (both
    `ROOT / "generated"`).

    `workspace_home.GENERATED_DIR` is a normal cached import — `app.py`'s
    `from command_center import workspace_home` resolves to the same `sys.modules`
    entry patched below, so patching it here holds everywhere, including inside a
    Streamlit `AppTest` run.

    `app.GENERATED_DIR` cannot be isolated the same way: `AppTest.from_file` re-execs
    `app.py`'s entire source fresh into a brand-new module registered as
    `sys.modules["__main__"]` on *every* `.run()` (see
    `streamlit/runtime/scriptrunner/script_runner.py`: `module = self._new_module
    ("__main__")` then `sys.modules["__main__"] = module`). A separate `import app`
    here is therefore a different module object than the one AppTest actually
    executes, so `monkeypatch.setattr(app, "GENERATED_DIR", ...)` would silently do
    nothing for any AppTest-driven test. It is still patched below for the benefit of
    tests that `import app` directly instead of going through `AppTest` (the pattern
    `test_kanban_page_registry_includes_aicos_with_no_local_projects_dict` already
    uses for `app.PROJECTS`).

    The actual leak into the real `generated/AIOS/` came from a different place:
    `app.py`'s task-creation form calls `run_start_task_script`, which shells out to
    `scripts/start-task.sh` — that script resolves its own `ROOT_DIR` from
    `${BASH_SOURCE[0]}`'s on-disk location, bypassing `GENERATED_DIR` in either module
    entirely. Patching the two `GENERATED_DIR` constants alone would leave every
    task-creation test still invoking the real script and writing straight into the
    developer's real `generated/AIOS/` (confirmed root cause: every stray
    `*_implementation.md` there with objective text `"Do the overridden thing"` /
    `"Do the inherited thing"` came from exactly this path — see
    `test_app_streamlit.py`'s `test_created_task_inherits_project_workspace_and_branch_without_manual_entry`
    / `test_created_task_override_wins_over_project_inheritance`). `subprocess.run` is
    therefore guarded here too: any call whose command targets `start-task.sh` is
    redirected to write its Markdown output under the isolated directory instead of
    shelling out for real. `subprocess` is stdlib, always the same cached module
    object process-wide, so this interception holds regardless of how/when the
    calling code (including a freshly re-exec'd `app.py`) imported it — the same
    technique `fake_claude` already uses for `supervisor_module.subprocess.Popen`.
    Everything that isn't a `start-task.sh` call is forwarded to the real
    `subprocess.run` unchanged (e.g. `git_repo`'s real `git init`).

    Guarded like the streamlit cleanup above, and for the same host: `app.py`
    hard-imports streamlit at line 10, so on a headless worker machine this
    autouse fixture would fail setup for every test in the tree — including
    the worker-daemon tests whose whole point is running where the desktop
    does not. No streamlit means no `app`, means nothing here to isolate."""
    try:
        import app
    except ImportError:
        yield
        return
    from command_center import workspace_home

    generated_dir = isolated_data_dir / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app, "GENERATED_DIR", generated_dir)
    monkeypatch.setattr(workspace_home, "GENERATED_DIR", generated_dir)

    original_run = subprocess.run

    def guarded_run(command, *args, **kwargs):
        argv = list(command) if isinstance(command, (list, tuple)) else [command]
        if argv and str(argv[0]).endswith("start-task.sh"):
            project = argv[1] if len(argv) > 1 else "AIOS"
            task_type = argv[2] if len(argv) > 2 else "implementation"
            objective = argv[3] if len(argv) > 3 else ""
            target_dir = generated_dir / project.upper()
            target_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (target_dir / f"{timestamp}_{task_type.lower()}.md").write_text(
                "# Agent Task (test double written by isolated_generated_dir)\n\n"
                f"Project: {project.upper()}\nTask type: {task_type.lower()}\n\n"
                f"## Objective\n\n{objective}\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    yield generated_dir


@pytest.fixture(autouse=True)
def forbid_real_codex_subprocess(monkeypatch):
    """Fail immediately if any test invokes Codex outside its fake fixture."""
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def check(command):
        if isinstance(command, str):
            try:
                command = shlex.split(command)
            except ValueError as exc:
                raise AssertionError(f"Unparseable subprocess command in test: {command!r}") from exc
        if not isinstance(command, (list, tuple)) or not command:
            return
        executable = Path(os.fspath(command[0])).expanduser()
        if executable.name != "codex":
            return
        allowed = os.environ.get("AICC_TEST_FAKE_CODEX_BINARY")
        if allowed and executable.resolve() == Path(allowed).resolve():
            return
        # A capability probe is not an execution. `providers._probe` runs
        # `--version` and `exec --help` to decide whether a provider is usable
        # at all, and `scheduler.default_registry()` consults that for every
        # provider on every planning tick. Those read-only invocations spend no
        # quota, touch no repository and start no agent, so blocking them would
        # make the guard fail honest planning code while protecting nothing.
        # Anything that could actually run a task is still refused.
        probe_args = tuple(str(arg) for arg in command[1:])
        if probe_args in (("--version",), ("exec", "--help")):
            return
        raise AssertionError(f"Automated test attempted to invoke non-fixture Codex: {executable}")

    def guarded_run(command, *args, **kwargs):
        check(command)
        return real_run(command, *args, **kwargs)

    def guarded_popen(command, *args, **kwargs):
        check(command)
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)


_CONTAMINATION_MARKERS: tuple[str, ...] = (
    "pytest-of-",
    "aicc_test_data_",
    "-fake-repo",
)


@pytest.fixture(scope="session", autouse=True)
def guard_real_project_files():
    """Session-wide regression guard for the exact failure this fixture module
    responds to: a pytest-generated temp path (`tmp_path`, `pytest-of-<user>/
    pytest-<N>/...`) or an isolated test data dir path ending up persisted inside the
    developer's real `data/*.json(l)`/`data/project_config.json` or `reports/`
    content. Scans both trees for `_CONTAMINATION_MARKERS` once at session end and
    fails loudly, naming the exact file and matched marker, if any are found.

    Deliberately **not** a byte-for-byte before/after diff: this repo is a
    self-hosted dev tool the developer routinely runs live (`streamlit run app.py`)
    while also running the test suite, and that live process legitimately rewrites
    `data/tasks.json`/`data/execution_queue.json`/`data/activity.jsonl` on its own
    schedule (Live Execution Center polling, queue re-evaluation) — a strict diff
    guard flags that normal concurrent usage as a false positive (confirmed while
    building this fixture: a live `streamlit run app.py` process was mutating
    `execution_queue.json`'s `evaluated_at` timestamps mid test-run, unrelated to the
    test suite). Marker-scanning targets the actual contamination signature instead
    of "any change," so it stays silent for legitimate concurrent app activity and
    loud only for genuine test-fixture leakage."""
    root = Path(__file__).resolve().parent.parent
    yield

    reports_root = root / "reports"

    def _scan(directory: Path) -> list[str]:
        if not directory.is_dir():
            return []
        hits = []
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            haystacks = [str(path)]
            # Agent-generated report .md files are expected to contain arbitrary
            # text (tool call logs, file paths, pytest references, etc.) — scan
            # their path for contamination markers but not their content.
            is_agent_report = reports_root in path.parents and path.suffix == ".md"
            if not is_agent_report and path.suffix in (".json", ".jsonl", ".md"):
                try:
                    haystacks.append(path.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
            for haystack in haystacks:
                for marker in _CONTAMINATION_MARKERS:
                    if marker in haystack:
                        hits.append(f"{path}: matched {marker!r}")
                        break
        return hits

    hits = _scan(root / "data") + _scan(root / "reports") + _scan(root / "generated")
    assert not hits, "Found test-fixture contamination in real project files:\n" + "\n".join(hits)


# --------------------------------------------------------------------------
# v2 runtime (Session Supervisor) test fixtures
# --------------------------------------------------------------------------

FAKE_CLAUDE_SCRIPT = Path(__file__).parent / "fixtures" / "fake_claude.py"
FAKE_CODEX_SCRIPT = Path(__file__).parent / "fixtures" / "fake_codex.py"


@pytest.fixture
def git_repo(tmp_path):
    """A real, throwaway git repository — never one of the user's real projects."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Pin the initial branch to ``main`` explicitly: the launch path's
    # workspace gate (workspace_provisioning.verify_workspace, step
    # ``base_branch_exists``) checks ``base_branch`` (defaults to ``main`` when
    # the project config omits it) resolves to a commit. On runners whose git
    # has no global ``init.defaultBranch=main`` (e.g. ubuntu-latest), a bare
    # ``git init`` creates ``master`` and that gate fails — diverging from local
    # runs where git defaults to ``main``. ``-b`` needs git >= 2.28, which every
    # supported runner ships.
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def configure_project_repo(monkeypatch):
    """Returns `configure(project_id, repo_path)`, patching project_config so
    that project resolves to `repo_path` for both `agent_runner.validate_repository`
    (used by `supervisor.Supervisor.start_raw`) and any direct caller."""
    from command_center import project_config

    def configure(project_id: str, repo_path: Path) -> None:
        original_get_project_config = project_config.get_project_config

        def fake_get_project_config(pid, _repo_path=str(repo_path), _project_id=project_id):
            cfg = original_get_project_config(pid)
            if pid == _project_id:
                cfg["repository_path"] = _repo_path
            return cfg

        monkeypatch.setattr(project_config, "get_project_config", fake_get_project_config)
        from command_center import agent_runner

        monkeypatch.setattr(agent_runner.project_config, "get_project_config", fake_get_project_config)

    return configure


@pytest.fixture
def fake_claude(monkeypatch):
    """Points `command_center.runtime.supervisor` at `tests/fixtures/fake_claude.py`
    (run under the *same* Python interpreter as the test) instead of the real
    `claude` binary, so supervisor tests exercise a genuine `subprocess.Popen`
    (real pid, real process group, real signal delivery) without ever invoking
    the real CLI or spending API credits. Returns a dict of env-var overrides
    the test can mutate before launching a run (see `fixtures/fake_claude.py`
    for the supported keys)."""
    from command_center.runtime import supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "CLAUDE_BINARY", sys.executable)

    original_build = supervisor_module.build_claude_command

    def patched_build(**kwargs):
        command = original_build(**kwargs)
        return [command[0], str(FAKE_CLAUDE_SCRIPT)] + command[1:]

    monkeypatch.setattr(supervisor_module, "build_claude_command", patched_build)

    # Defaults to touching a file every run, so a plain "implementation"-type
    # fake run genuinely changes the working tree — matching what a real
    # implementation run is expected to do, and what `runtime.outcome.
    # classify_process_result` now requires (`REQUIRES_CHANGES_TASK_TYPES`)
    # before it will classify an `exit_code == 0` implementation/remediation
    # run `COMPLETED` rather than `INCOMPLETE`. A test that specifically
    # wants to exercise the unchanged-working-tree path sets this back to
    # `""` (falsy — see `fixtures/fake_claude.py`'s `if touch_file:` guard).
    env_overrides: dict[str, str] = {"FAKE_CLAUDE_TOUCH_FILE": "fake_claude_default_touch.txt"}
    original_popen = subprocess.Popen

    def popen_with_env(*args, **kwargs):
        env = dict(os.environ)
        env.update(env_overrides)
        kwargs["env"] = env
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", popen_with_env)

    return env_overrides


@pytest.fixture
def fake_codex(monkeypatch, tmp_path):
    """Install an isolated executable Codex double and route every probe/run to it."""
    from command_center import project_config

    executable = tmp_path / "codex"
    executable.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CODEX_SCRIPT} \"$@\"\n")
    executable.chmod(0o700)
    monkeypatch.setenv("AICC_CODEX_BINARY", str(executable))
    monkeypatch.setenv("AICC_TEST_FAKE_CODEX_BINARY", str(executable))
    monkeypatch.setenv("FAKE_CODEX_TOUCH_FILE", "fake_codex_default_touch.txt")
    project_config.save_allowed_agents("AIOS", ["claude_code", "codex"])
    return executable


FAKE_COPILOT_SCRIPT = Path(__file__).parent / "fixtures" / "fake_copilot.py"


@pytest.fixture
def fake_copilot(monkeypatch, tmp_path):
    """Install an isolated executable Copilot CLI double and route every probe/run to it."""
    from command_center import project_config

    executable = tmp_path / "copilot"
    executable.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_COPILOT_SCRIPT} \"$@\"\n")
    executable.chmod(0o700)
    monkeypatch.setenv("AICC_COPILOT_BINARY", str(executable))
    monkeypatch.setenv("FAKE_COPILOT_TOUCH_FILE", "fake_copilot_default_touch.txt")
    project_config.save_allowed_agents("AIOS", ["claude_code", "copilot_cli"])
    return executable


FAKE_CLAUDE_TREE_SCRIPT = Path(__file__).parent / "fixtures" / "fake_claude_tree.py"


@pytest.fixture
def fake_claude_tree(monkeypatch, tmp_path):
    """Like `fake_claude`, but points at `fixtures/fake_claude_tree.py`, which
    spawns a real parent -> child -> grandchild process tree in one process
    group (F4: grandchild cancellation regression coverage). Returns
    `(env_overrides, pidfile_base)` — `pidfile_base + ".parent"/".child"/
    ".grandchild"` are written once the whole tree is up."""
    from command_center.runtime import supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "CLAUDE_BINARY", sys.executable)

    original_build = supervisor_module.build_claude_command

    def patched_build(**kwargs):
        command = original_build(**kwargs)
        return [command[0], str(FAKE_CLAUDE_TREE_SCRIPT)] + command[1:]

    monkeypatch.setattr(supervisor_module, "build_claude_command", patched_build)

    pidfile_base = str(tmp_path / "tree_pids")
    env_overrides: dict[str, str] = {"FAKE_CLAUDE_TREE_PIDFILE": pidfile_base}
    original_popen = subprocess.Popen

    def popen_with_env(*args, **kwargs):
        env = dict(os.environ)
        env.update(env_overrides)
        kwargs["env"] = env
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", popen_with_env)

    return env_overrides, pidfile_base
