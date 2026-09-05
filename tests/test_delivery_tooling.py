"""The gates that check other work, checked themselves.

Both tools in this file's scope shipped without a test, and independent
acceptance immediately found each of them reporting success it had not
measured: the sweep called every hook "caught" against an already-red suite,
and the evidence check exited 0 over a failing run and over a message whose
claim it could not parse. Tooling whose whole purpose is to refuse a false
claim is the last place to accept one, so every attack that worked is a test
here.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "scripts" / "evidence.py"
SLICE_CHECKS = ROOT / "scripts" / "mirror_slice_checks.py"


def _write_suite(directory: Path, *, green: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    body = "def test_ok():\n    assert True\n" if green else (
        "def test_ok():\n    assert True\n\n\ndef test_broken():\n    assert False, 'on fire'\n"
    )
    path = directory / "test_probe.py"
    path.write_text(body, encoding="utf-8")
    return path


def _run(script: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a tool and keep its stderr with its stdout in the failure message.

    The first version asserted only on stdout, so when the tool did not run at
    all on CI — it hard-coded `uv`, which the runner does not have — every
    assertion failed as `assert 'FAILED' in ''`. An empty string is the least
    informative way to learn that a subprocess died, and it cost a full gate
    round to find out why.
    """
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.stdout or completed.returncode == 0, (
        f"{script.name} produced no stdout (exit {completed.returncode}); stderr:\n"
        f"{completed.stderr[-2000:]}"
    )
    return completed


def test_evidence_check_fails_on_a_red_suite_even_when_the_numbers_agree(tmp_path) -> None:
    """The shape acceptance used: a claim that matches, over a suite that fails.

    `1 passed / 0 skipped` is literally true of the run below, and the run also
    contains a failure. Comparing only the two numbers made the tool agree with
    a sentence while the thing it describes was broken.
    """
    suite = _write_suite(tmp_path / "red", green=False)
    message = tmp_path / "message.txt"
    message.write_text(
        f"fix: something\n\nEvidence: `{suite}` 1 passed / 0 skipped; ruff clean.\n",
        encoding="utf-8",
    )

    result = _run(EVIDENCE, "check", "--message-file", str(message), str(suite))
    assert result.returncode != 0, result.stdout
    assert "FAILED" in result.stdout


def test_evidence_check_fails_when_it_cannot_find_the_claim(tmp_path) -> None:
    """"I could not find the claim" is not "the claim is right".

    A message with no `Evidence:` line gives a reviewer nothing to check
    either, so stopping is the useful answer rather than a silent zero.
    """
    suite = _write_suite(tmp_path / "green", green=True)
    message = tmp_path / "message.txt"
    message.write_text("fix: something\n\nno numbers here at all\n", encoding="utf-8")

    result = _run(EVIDENCE, "check", "--message-file", str(message), str(suite))
    assert result.returncode != 0
    assert "no `Evidence:" in result.stdout


def test_evidence_check_ignores_counts_that_are_not_the_evidence_line(tmp_path) -> None:
    """A message may legitimately discuss other counts.

    Corrections quote what a number used to be, and examples show what a
    mismatch looks like. Matching those against the measured suite made the
    tool fire on messages that were entirely honest — and a gate that always
    fires is one people learn to skip.
    """
    suite = _write_suite(tmp_path / "green", green=True)
    message = tmp_path / "message.txt"
    message.write_text(
        "fix: something\n\n"
        "The previous message said 527 passed / 40 skipped, which was stale.\n\n"
        f"Evidence: `{suite}` 1 passed / 0 skipped; ruff clean.\n",
        encoding="utf-8",
    )

    result = _run(EVIDENCE, "check", "--message-file", str(message), str(suite))
    assert result.returncode == 0, result.stdout
    assert result.stdout.count("[ok]") == 1


def test_evidence_check_reports_a_drifted_claim(tmp_path) -> None:
    suite = _write_suite(tmp_path / "green", green=True)
    message = tmp_path / "message.txt"
    message.write_text(
        f"fix: something\n\nEvidence: `{suite}` 99 passed / 3 skipped; ruff clean.\n",
        encoding="utf-8",
    )

    result = _run(EVIDENCE, "check", "--message-file", str(message), str(suite))
    assert result.returncode != 0
    assert "MISMATCH" in result.stdout


def test_the_sweep_refuses_to_run_against_a_red_suite(tmp_path) -> None:
    """The finding that mattered most, because it inverted the tool's meaning.

    With no baseline, `caught = returncode != 0` cannot distinguish "the
    perturbation was noticed" from "the suite was already failing", so a red
    suite reported every hook as covered — the tool announcing the coverage it
    was built to doubt.
    """
    suite = _write_suite(tmp_path / "red", green=False)

    result = _run(
        SLICE_CHECKS,
        "sweep",
        "command_center/runtime/db/completion.py",
        "--suite",
        str(suite),
    )
    assert result.returncode != 0, result.stdout
    assert "already failing" in result.stdout
    assert "caught" not in result.stdout, "a red baseline must never produce a coverage verdict"


def test_the_sweep_leaves_the_tree_untouched(tmp_path) -> None:
    """Including when it refuses: a probe that can leave the tree perturbed
    will one day be blamed for a real failure."""
    suite = _write_suite(tmp_path / "red", green=False)
    target = ROOT / "command_center/runtime/db/completion.py"
    before = target.read_text(encoding="utf-8")

    _run(SLICE_CHECKS, "sweep", "command_center/runtime/db/completion.py", "--suite", str(suite))

    assert target.read_text(encoding="utf-8") == before


def test_measure_emits_a_line_check_can_read_back(tmp_path) -> None:
    """The documented workflow is measure, paste, check — so it must round-trip.

    `measure` used to omit `0 skipped` while `check`'s pattern required it, so
    on any suite with no skips the tool produced a line its own reader called
    unparseable. Fails closed, so it was safe — and useless, which is how a
    gate stops being run.
    """
    suite = _write_suite(tmp_path / "green", green=True)
    measured = _run(EVIDENCE, "measure", str(suite))
    assert measured.returncode == 0, measured.stdout

    message = tmp_path / "message.txt"
    message.write_text(f"fix: something\n\n{measured.stdout.strip()}\n", encoding="utf-8")

    checked = _run(EVIDENCE, "check", "--message-file", str(message), str(suite))
    assert checked.returncode == 0, checked.stdout
    assert "[ok]" in checked.stdout


def test_the_line_records_which_configuration_measured_it(tmp_path) -> None:
    """Two honest runs of one tree differ by whether a database was reachable.

    The warning about that went to stderr, which nobody pastes into a commit
    message, so the two lines were indistinguishable in the place they are
    read. The configuration now travels with the numbers.

    Both configurations are set explicitly rather than inherited. The first
    version asserted `serverless` and inherited whatever the developer had
    exported — so it passed on a laptop with no database and failed on one with
    it, which is the very ambiguity the feature exists to remove.
    """
    suite = _write_suite(tmp_path / "green", green=True)

    serverless = {k: v for k, v in os.environ.items() if k != "AICC_TEST_PG_ADMIN_DSN"}
    measured = _run(EVIDENCE, "measure", str(suite), env=serverless)
    assert "(serverless)" in measured.stdout, measured.stdout

    with_database = {**os.environ, "AICC_TEST_PG_ADMIN_DSN": "host=127.0.0.1 dbname=irrelevant"}
    measured = _run(EVIDENCE, "measure", str(suite), env=with_database)
    # `(PostgreSQL requested` without its closing paren, deliberately. The
    # marker gained a third state — `PostgreSQL requested, **driver missing**`
    # — and the exact-substring form made this test host-dependent again on any
    # machine without `psycopg`: the same defect its own docstring above
    # condemns, reintroduced by the commit that added the state. What this test
    # is about is that a requested database is distinguishable from a
    # serverless run; which of the two requested states appears is
    # `test_the_marker_names_a_missing_driver_...`'s subject, where it is set
    # explicitly rather than inherited.
    assert "(PostgreSQL requested" in measured.stdout, measured.stdout
    assert "(serverless)" not in measured.stdout, measured.stdout


def test_a_missing_ruff_is_never_reported_as_a_dirty_tree() -> None:
    """The third state has to hold on both branches, and twice it did not.

    Exercised through `_ruff()` with a synthesised result rather than by
    stripping `PATH`. The first version did the latter and review showed it
    proved nothing: CI installs ruff into the very interpreter that runs
    pytest, so the fallback branch is never taken there, and the assertion
    passed with the fix reverted. Worse, on a host where ruff is importable and
    the tree is dirty it failed for a reason unrelated to its own claim.

    The discriminator is the thing under test, so the test calls it directly.
    """
    import importlib.util
    import subprocess

    spec = importlib.util.spec_from_file_location("evidence_under_test", EVIDENCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    shapes = {
        "missing under `python -m ruff`": subprocess.CompletedProcess(
            [], 1, stdout="", stderr="/usr/bin/python3: No module named ruff\n"
        ),
        "missing under `uv run`": subprocess.CompletedProcess(
            [], 2, stdout="", stderr="error: Failed to spawn: `ruff`\n"
        ),
        "misconfigured": subprocess.CompletedProcess(
            [], 2, stdout="", stderr="ruff failed\n  Cause: Unknown rule selector\n"
        ),
        "killed": subprocess.CompletedProcess([], -9, stdout="", stderr=""),
    }
    for name, completed in shapes.items():
        assert module._classify_ruff(completed) == "unavailable", name

    # And the direction that must never be lost: real findings stay `dirty`.
    dirty = subprocess.CompletedProcess([], 1, stdout="F401 unused import\n", stderr="")
    assert module._classify_ruff(dirty) == "dirty"
    assert module._classify_ruff(subprocess.CompletedProcess([], 0, stdout="", stderr="")) == "clean"


@contextlib.contextmanager
def _forced_uv(module, *, present: bool):
    """Run a block as if `uv` were present or absent, whatever the host has.

    Both branches must be asserted everywhere, because the two machines
    disagree: this laptop has uv, and CI has none at all. Letting each host
    check only its own branch left the uv form unguarded exactly where it
    mattered.
    """
    original = module.shutil.which
    module.shutil.which = lambda name: "/usr/bin/uv" if present else None
    try:
        yield
    finally:
        module.shutil.which = original


def _evidence_module():
    """Import the tool as a module so its internals can be exercised directly.

    Loading it by path rather than adding `scripts/` to `sys.path`: the tool is
    a script, not a package, and making it importable by side effect would be a
    change to the thing under test.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("evidence_under_test", EVIDENCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_marker_names_a_missing_driver_instead_of_reading_as_a_postgres_run(
    monkeypatch,
) -> None:
    """The shape that twice survived a claim that it could not exist.

    With the DSN set and `psycopg` absent, every database-backed test skips
    silently: no FAILED term, exit 0, under a line reading `PostgreSQL
    requested`. Two successive comments asserted this was impossible — the
    second written in the commit retracting the first — and review reproduced
    it both times, the second through the pip fallback branch, which pins
    nothing.

    So the line stops asserting and starts asking.
    """
    module = _evidence_module()
    counts = {"passed": 402, "skipped": 260}
    monkeypatch.setenv("AICC_TEST_PG_ADMIN_DSN", "host=127.0.0.1 port=1 dbname=x")

    monkeypatch.setattr(module, "_driver_reachable", lambda: False)
    unreachable = module._evidence_line(["tests/db"], counts, "clean")
    assert "driver missing" in unreachable

    monkeypatch.setattr(module, "_driver_reachable", lambda: True)
    reachable = module._evidence_line(["tests/db"], counts, "clean")
    # Exact, not a substring. Review pointed out that a *new* suffix — say
    # `PostgreSQL requested, **connection refused**` — passes every substring
    # assertion in this file, so the marker could gain a meaning nothing here
    # would notice.
    assert reachable.endswith("(PostgreSQL requested).")

    monkeypatch.delenv("AICC_TEST_PG_ADMIN_DSN")
    assert "serverless" in module._evidence_line(["tests/db"], counts, "clean")


def test_the_driver_probe_and_the_test_command_pin_the_same_extras() -> None:
    """Drift between them would be invisible.

    The probe answers "can the driver be imported" for whichever environment
    it pins. If that stops being the environment the tests run in, the probe
    still answers — about nothing — and the marker goes back to being a
    statement about hope.
    """
    module = _evidence_module()

    # Both branches are forced, on every host. Letting each host check only its
    # own was the deeper version of the same defect: CI installs **no** uv at
    # all — `grep -icE "astral|setup-uv|uv (run|sync|pip|venv)" ci.yml` is 0,
    # every job installs with `pip --require-hashes` — so the uv branch this
    # test guards was never executed by the gate, and review demonstrated both
    # of the previous round's mutations passing green there. A test that skips
    # on the machine that decides the merge is not a test on that machine.
    with _forced_uv(module, present=True):
        command = module._pytest_command(["tests/db"], [])
    for token in module._DB_EXTRAS:
        assert token in command, f"test command lost {token}"

    # Both sides. The first version asserted only on the test command, so
    # review replaced the *probe's* extras with an unrelated package and this
    # test — the one named for that exact drift — passed.
    # Forced too, for the same reason: on a host without uv the probe takes the
    # fallback form, which pins nothing by design, and comparing it against
    # `_DB_EXTRAS` asserts something that was never true there. That is what
    # this test did on the CI shape until the forcing was added above and not
    # here — half a fix, which is how the whole uv branch went unguarded on the
    # gate in the first place.
    with _forced_uv(module, present=True):
        probe = module._driver_probe_command()
    for token in module._DB_EXTRAS:
        assert token in probe, f"probe command lost {token}"


def test_the_probe_asks_about_the_driver_the_tests_actually_need(tmp_path) -> None:
    """What the probe *asks* is the fix; two tests bound only how it is spelled.

    Review changed `import psycopg` to `import sys` in both branches and the
    whole file stayed green — while the tool then printed a bare
    `(PostgreSQL requested)` over 260 silent skips, which is the round-three
    defect verbatim. The probe is the fix here exactly as `_classify_ruff` was,
    and that one was blocked for the same reason: a fix no test holds.
    """
    module = _evidence_module()
    assert "import psycopg" in module._driver_probe_command()

    # And the behaviour, not only the spelling: blocked at import, the probe
    # must say so; unblocked, it must not.
    #
    # `tmp_path`, not a directory beside this file. The first version wrote a
    # module *named `psycopg.py`* into `tests/fixtures/` and never removed it,
    # so every run left an untracked file in the source tree — in a PR whose
    # own body explains that `git add -A` swept a stray file in three times.
    blocker = tmp_path / "block_psycopg"
    blocker.mkdir()
    (blocker / "psycopg.py").write_text('raise ImportError("blocked")\n', encoding="utf-8")
    # The behavioural half runs the *fallback* form — `sys.executable -c` —
    # deliberately, and this is a limit worth stating rather than hiding. The
    # first version ran `_driver_probe_command()` itself, which on a runner with
    # `uv` means `uv run --with psycopg[binary] …`: an environment build, twice,
    # inside a test. CI cancelled the job at its 30-minute ceiling against a
    # 14-minute norm. What is exercised here is that the probe's *question*
    # answers to reality; that the uv form asks the same question is the
    # assertion above and `..._pin_the_same_extras`.
    fallback = [sys.executable, "-c", "import psycopg"]
    with _forced_uv(module, present=True):
        uv_probe = module._driver_probe_command()
    with _forced_uv(module, present=False):
        probe = module._driver_probe_command()
    # The **whole** argv, not its tail. Comparing `[-2:]` pinned `-c` and the
    # import and left the executable and the interpreter free, and review
    # showed what that costs. Dropping `"python"` — a plausible edit, thinking
    # `-c` belongs to uv — makes the probe fail always, so every run reads
    # `driver missing` including against a database it reached. Replacing
    # `"uv"` with anything that exits 0 makes it succeed always, which is the
    # bare `(PostgreSQL requested)` over 260 silent skips: the exact defect
    # three rounds went into closing, restored with the file green.
    #
    # An argv comparison costs no runtime, so the CI-cost objection that moved
    # the behavioural half to the fallback form does not apply here.
    assert uv_probe == ["uv", "run", *module._DB_EXTRAS, "python", "-c", "import psycopg"]
    assert probe == fallback
    existing = os.environ.get("PYTHONPATH")
    blocked = subprocess.run(
        fallback,
        cwd=ROOT,
        capture_output=True,
        env={
            **os.environ,
            # Prepended, not replacing: dropping an inherited PYTHONPATH would
            # change more than the one import under test.
            "PYTHONPATH": os.pathsep.join([str(blocker), *([existing] if existing else [])]),
        },
    )
    assert blocked.returncode != 0, "the probe reported a driver that cannot be imported"

    # And the other direction, which needs the driver importable *by the
    # interpreter the probe runs*. `pytest.importorskip` asks this process
    # instead, and the two are not the same question: an interpreter paired
    # with another version's site-packages imports `psycopg` here and fails
    # there on the compiled extension. Ask the subprocess.
    # Skip only when the driver is genuinely absent; fail when it is installed
    # and the probe's own command still cannot import it. The previous form
    # skipped on *any* non-zero exit and then asserted `returncode == 0` on the
    # only path that could not reach it — a check that cannot fire, which is the
    # shape `mirror_slice_checks.py`'s own docstring warns about.
    unblocked = subprocess.run(fallback, cwd=ROOT, capture_output=True)
    if unblocked.returncode != 0:
        if importlib.util.find_spec("psycopg") is None:
            pytest.skip("psycopg is not installed here; the probe is right to say so")
        raise AssertionError(
            "psycopg is installed and the probe's own command still cannot "
            f"import it:\n{unblocked.stderr.decode(errors='replace')[-2000:]}"
        )


def test_the_slice_sweep_resolves_pytest_the_same_way_the_evidence_tool_does() -> None:
    """The one change in this PR that review found had no test at all.

    `mirror_slice_checks.py` hard-coded `pytest` from PATH while its comment
    pointed at `evidence.py` "for why the hard-coded form was a defect". The
    code now matches the comment; this is what holds it there.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("slice_checks_under_test", SLICE_CHECKS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with _forced_uv(module, present=True):
        command = module._pytest_command("tests/db")
    with _forced_uv(module, present=False):
        fallback_command = module._pytest_command("tests/db")
    assert fallback_command[0] == sys.executable and fallback_command[1:3] == ["-m", "pytest"]
    # `pytest` in the `--with` set is the whole fix: without it uv resolves
    # pytest from PATH, the console script re-pins `sys.prefix`, and the extra
    # never reaches the interpreter that runs the tests.
    assert command[:2] == ["uv", "run"]
    pinned = [command[i + 1] for i, token in enumerate(command) if token == "--with"]

    # The first version of this test asserted on two substrings that were both
    # already present in the *unfixed* file, so it passed against the very
    # defect it was named for: review restored the buggy source wholesale and
    # watched it go green. Asserting on the command makes that impossible.
    assert "pytest" in pinned, command


def test_the_slice_sweep_pins_the_driver_the_suite_it_sweeps_needs() -> None:
    """`cmd_sweep`'s own baseline could read green while covering nothing.

    `_pytest_command` pinned `psycopg-pool` for its `uv` branch but not
    `psycopg` itself, while `tests/db/conftest.py` gates every PostgreSQL test
    on `pytest.importorskip("psycopg")` — a driver `psycopg-pool` does not
    depend on. Under `uv`, with no project-level psycopg installed, every
    database-backed test would skip silently: baseline exits 0, every
    perturbation also exits 0, and `cmd_sweep` reports every hook UNNOTICED —
    not because nothing covers it, but because the sweep's own command could
    never have reached a covering test in the first place. `evidence.py`
    pins both `psycopg[binary]` and `psycopg-pool` for exactly this suite, in
    a list named `_DB_EXTRAS` specifically so a second, independent copy
    could not drift from it — and this file was that second copy.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("slice_checks_under_test", SLICE_CHECKS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evidence = _evidence_module()

    with _forced_uv(module, present=True):
        command = module._pytest_command("tests/db")
    pinned = [command[i + 1] for i, token in enumerate(command) if token == "--with"]

    # Every extra `evidence.py` pins for this exact suite, not just the one
    # that happens to share a name with what this file already had.
    expected = [
        token for i, token in enumerate(evidence._DB_EXTRAS) if evidence._DB_EXTRAS[i - 1] == "--with"
    ]
    for extra in expected:
        assert extra in pinned, f"{extra!r} missing from the sweep's pytest command: {command}"


def test_the_statements_probe_finds_a_delete_by_the_capability_not_the_name() -> None:
    """`collect_statements` claimed to probe whole-predicate deletes "wherever
    a store exposes it rather than naming it", while the code named exactly
    one: `for extra in ("delete_day",):`. `PostgresTaskMirror.delete_task`
    existed the whole time, was never in that tuple, and so `cmd_statements`'s
    diff had zero signal for changes to its generated SQL — a second,
    unnamed instance of the same defect this file's own docstring calls out
    for `--root` inference. `task` mirroring only `upsert` and `list_records`
    (2 statements, not 3) is exactly what that blind spot looked like.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("slice_checks_under_test", SLICE_CHECKS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    statements = module.collect_statements()
    assert len(statements["task"]) == 3, (
        f"expected upsert + list_records + delete_task, got {statements['task']}"
    )
