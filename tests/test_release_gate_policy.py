"""Exact, machine-checkable contract for merge-relevant workflow contexts."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
BOUNDARY_WORKFLOW = ROOT / ".github/workflows/arch-fitness.yml"

EXPECTED_CONTEXTS = {
    "prepare": "Prepare CI shared inputs",
    "quality-gates": "Linux quality shard ${{ matrix.shard }} of 4",
    "manifest-gate": "Linux manifest gate (exactly once)",
    "coverage-gate": "Main coverage aggregation",
    "windows-quality-gates": "Windows quality gates (Ruff · compile · pytest)",
    "security-gates": "Security gates (workflow policy · provenance · supply chain)",
    "build-gates": "Build gates (web production)",
    # Advisory-only fast pre-check (dependency-based test-impact selection). It is
    # deliberately NOT wired into `final-gate.needs` (see
    # `test_final_gate_is_fail_closed_for_every_upstream_result`), so it can never
    # become the sole gate and never reduces coverage.
    "impact-fast-check": "Impact fast pre-check (advisory)",
    "final-gate": "Final merge gate",
    "boundary-fitness": "Boundary fitness (import ban · anti-engine baseline)",
}

EXPECTED_STEPS = {
    # Four fixed shards run the core partition while exactly one matrix member
    # owns each non-parallel partition. The manifest gate proves the nodeid
    # union is non-empty and exactly-once before Final can pass.
    "quality-gates": {
        "Pytest core shard",
        "Pytest (serial tail)",
        "Real-browser E2E",
        "Publish actual test collection receipt",
    },
    "prepare": {
        "Fetch and verify exact accepted AIOS SDK and DB artifacts",
        "Build exactly-once test manifest",
        "Publish prepared SDK, DB, and test manifest",
    },
    "manifest-gate": {"Verify actual collections equal planned partition"},
    "coverage-gate": {"PR and merge-group coverage policy"},
    "impact-fast-check": {
        "Select impacted tests",
        "Run impacted tests (parallel body + serial tail)",
    },
    "windows-quality-gates": {"Desktop pytest-qt suite", "Real-browser E2E"},
    "security-gates": {
        "Release gate policy",
        "Build Python dependency SBOM",
        "Critical/high vulnerability scan (pip-audit)",
        "Secret scan",
        "Initialize CodeQL",
        "Autobuild for CodeQL",
        "Run CodeQL analysis",
        "Focused security regressions",
    },
    "build-gates": {"Web production build"},
    "final-gate": {"Assert required checks"},
    "boundary-fitness": {"AIOS boundary fitness tests"},
}

CANARY_LABELS = {
    "quality-gates": "release-gate-canary-linux",
    "windows-quality-gates": "release-gate-canary-windows",
    "security-gates": "release-gate-canary-security",
    "build-gates": "release-gate-canary-build",
    "boundary-fitness": "release-gate-canary-boundary",
}


def _workflow(path: Path) -> dict:
    # BaseLoader keeps the YAML 1.1 word ``on`` as a string instead of turning
    # it into True, while structure is all this policy test needs.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _all_jobs() -> dict[str, dict]:
    jobs = {}
    for path in (CI_WORKFLOW, BOUNDARY_WORKFLOW):
        jobs.update(_workflow(path)["jobs"])
    return jobs


def test_release_context_names_and_workflow_coverage_are_exact() -> None:
    ci = _workflow(CI_WORKFLOW)
    boundary = _workflow(BOUNDARY_WORKFLOW)
    jobs = {**ci["jobs"], **boundary["jobs"]}

    assert set(ci["jobs"]) == set(EXPECTED_CONTEXTS) - {"boundary-fitness"}
    assert set(boundary["jobs"]) == {"boundary-fitness"}
    assert {job_id: job["name"] for job_id, job in jobs.items()} == EXPECTED_CONTEXTS
    assert ci["jobs"]["quality-gates"]["needs"] == "prepare"
    assert set(ci["jobs"]["manifest-gate"]["needs"]) == {
        "prepare",
        "quality-gates",
    }

    # The exact trigger set, and it is a security statement rather than
    # bookkeeping: every entry here is a context in which these gates run with
    # the repository's own token, so a trigger added without review is a new
    # way to reach that token. This test caught `merge_group` being added and
    # made the addition deliberate, which is the whole point of pinning it.
    #
    # `merge_group` is admitted because the merge queue is where the required
    # gates must run once the queue is enabled: a workflow that does not
    # subscribe to it never reports there and every queue entry times out.
    #
    # **What is NOT true, and was written here before independent acceptance
    # corrected it: that the queue "never runs a fork's code".** The queue ref
    # (`gh-readonly-queue/<base>`) is created in this repository, but it
    # carries the pull request's own commits — a fork's commits included. And
    # because the event is a base-repository event rather than a
    # fork-originated one, **secrets are passed**, where the `pull_request`
    # path gives a fork an empty secret and a read-only token.
    #
    # That matters concretely here: `ci.yml` runs
    # `scripts/fetch_aios_sdk_artifact.py` — a script from the checked-out
    # tree — with `AIOS_ARTIFACT_READ_TOKEN` in its environment, and that token
    # reads the *private* `dimastov-lab/aios` repository. The need is not
    # removable while AIOS is private, so the exposure is closed at the step
    # instead: see
    # `test_every_step_holding_a_repository_secret_first_proves_its_code_is_ours`
    # (`VOYN-W0-AICC-MERGE-QUEUE-FORK-POLICY`).
    for workflow in (ci, boundary):
        assert set(workflow["on"]) == {
            "pull_request",
            "merge_group",
            "push",
            "workflow_dispatch",
        }
        assert set(workflow["on"]["pull_request"]["types"]) == {
            "opened",
            "synchronize",
            "reopened",
            "labeled",
            "unlabeled",
        }
        if workflow is ci:
            assert workflow["permissions"] == {
                "contents": "read",
                "security-events": "write",
                # Read-only, and used by exactly one caller: the trust guard has
                # to resolve the queued pull request to learn its head
                # repository, which the `merge_group` payload omits.
                "pull-requests": "read",
            }
        else:
            assert workflow["permissions"] == {"contents": "read"}

    for job_id, required_steps in EXPECTED_STEPS.items():
        step_names = {step.get("name") for step in jobs[job_id]["steps"]}
        assert required_steps <= step_names


def test_impact_fast_check_uses_the_mandatory_two_phase_serial_split() -> None:
    impact = _workflow(CI_WORKFLOW)["jobs"]["impact-fast-check"]
    (step,) = [
        step
        for step in impact["steps"]
        if step.get("name") == "Run impacted tests (parallel body + serial tail)"
    ]
    command = step["run"]

    assert 'run_phase -q -m "not serial" -n auto --dist loadscope' in command
    assert "run_phase -q -m serial -p no:xdist" in command
    assert 'if [ "$rc" -eq 5 ]' in command
    assert 'if [ "${#selected[@]}" -eq 0 ]' in command
    assert 'read -r path || [ -n "$path" ]' in command
    assert '[ -n "$path" ] && selected+=("$path")' in command
    assert "xargs" not in "\n".join(
        line for line in command.splitlines() if not line.lstrip().startswith("#")
    )


def _impact_script() -> str:
    impact = _workflow(CI_WORKFLOW)["jobs"]["impact-fast-check"]
    (step,) = [
        step
        for step in impact["steps"]
        if step.get("name") == "Run impacted tests (parallel body + serial tail)"
    ]
    return step["run"]


def _run_impact_script(
    tmp_path: Path, selection: str, *, parallel_rc: int = 0, serial_rc: int = 0
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "pytest-calls.txt"
    fake_pytest = bin_dir / "pytest"
    fake_pytest.write_text(
        """#!/usr/bin/env bash
printf 'CALL\\n' >> "$PYTEST_CALLS"
printf '<%s>\\n' "$@" >> "$PYTEST_CALLS"
if [[ " $* " == *' -m not serial '* ]]; then
  exit "$PARALLEL_RC"
fi
exit "$SERIAL_RC"
""",
        encoding="utf-8",
    )
    fake_pytest.chmod(0o755)
    (tmp_path / "selected-tests.txt").write_text(selection, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTEST_CALLS": str(calls),
        "PARALLEL_RC": str(parallel_rc),
        "SERIAL_RC": str(serial_rc),
    }
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", _impact_script()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text(encoding="utf-8") if calls.exists() else ""


def test_impact_script_keeps_unterminated_last_path_and_skips_blank_lines(
    tmp_path: Path,
) -> None:
    result, calls = _run_impact_script(
        tmp_path, "tests/first.py\n\ntests/last.py"
    )

    assert result.returncode == 0, result.stderr
    assert calls.count("CALL\n") == 2
    assert calls.count("<tests/first.py>\n") == 2
    assert calls.count("<tests/last.py>\n") == 2
    assert "<>" not in calls


def test_impact_script_neutralizes_only_no_tests_collected(tmp_path: Path) -> None:
    no_tests, calls = _run_impact_script(
        tmp_path, "tests/only.py\n", parallel_rc=5, serial_rc=5
    )
    assert no_tests.returncode == 0
    assert calls.count("CALL\n") == 2

    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    failed, failed_calls = _run_impact_script(
        failed_dir, "tests/only.py\n", parallel_rc=1
    )
    assert failed.returncode == 1
    assert failed_calls.count("CALL\n") == 1


def test_impact_script_refuses_an_empty_selection(tmp_path: Path) -> None:
    result, calls = _run_impact_script(tmp_path, "\n\n")

    assert result.returncode == 1
    assert calls == ""


SECRET_REFERENCE = re.compile(r"\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)[^}]*\}\}")

# `GITHUB_TOKEN` is minted per run and already scoped by `permissions:`; it is
# not a standing credential and the guard itself needs it to do its work.
EPHEMERAL_SECRETS = {"GITHUB_TOKEN"}

TRUST_GUARD = "python scripts/assert_trusted_head_repository.py"

# Shells whose GitHub invocation aborts the step on a non-zero exit, so that a
# refusal on line one is actually the end of the step:
#
#   bash -> `bash --noprofile --norc -eo pipefail {0}`   (`-e` aborts)
#   sh   -> `sh -e {0}`                                  (`-e` aborts)
#
# `pwsh` is deliberately absent. GitHub runs it as `pwsh -command ". '{0}'"`
# with `$ErrorActionPreference = 'stop'` and a trailing `exit $LASTEXITCODE`,
# and in PowerShell a *native* command's non-zero exit is not a terminating
# error — `$PSNativeCommandUseErrorActionPreference` defaults to `$false`, and
# `ErrorActionPreference` does not cover it. Execution continues to the next
# line and the step's exit code is the last command's, so a refused guard
# leaves the step green. This is not hypothetical: `windows-quality-gates`
# declared no shell, took the `windows-latest` default of `pwsh`, and did
# exactly that.
ABORTING_SHELLS = {"bash", "sh"}


def _workflow_paths() -> list[Path]:
    """Every workflow in the directory.

    Derived from the filesystem rather than from a constant on purpose. The
    earlier version of the secret invariants walked two hard-coded paths, so a
    new `.github/workflows/anything.yml` subscribing to `merge_group` with an
    unguarded secret satisfied the whole suite.
    """
    directory = ROOT / ".github/workflows"
    paths = sorted(p for p in directory.iterdir() if p.suffix in {".yml", ".yaml"})
    assert paths, f"no workflows found under {directory}"
    return paths


def _secret_names(value: object) -> set[str]:
    """Every repository secret reachable from a YAML fragment."""
    if isinstance(value, str):
        return set(SECRET_REFERENCE.findall(value))
    if isinstance(value, dict):
        return set().union(set(), *(_secret_names(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(set(), *(_secret_names(item) for item in value))
    return set()


def _first_command(run: str) -> str:
    for line in run.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _secret_bearing_steps() -> list[tuple[Path, str, dict, dict]]:
    """(workflow, job id, job, step) for every step holding a standing secret."""
    found = []
    for path in _workflow_paths():
        workflow = _workflow(path)
        for job_id, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if _secret_names(step) - EPHEMERAL_SECRETS:
                    found.append((path, job_id, job, step))
    return found


def test_every_step_holding_a_repository_secret_first_proves_its_code_is_ours() -> None:
    """A standing credential may only be handed to code this repository wrote.

    The `merge_group` trigger runs in the base repository and therefore *is*
    given repository secrets, while the ref it builds carries the pull
    request's own commits — a fork's included. `AIOS_ARTIFACT_READ_TOKEN` reads
    the private `dimastov-lab/aios` repository, and it is handed to
    `scripts/fetch_aios_sdk_artifact.py`, a script read out of that same tree.
    The need cannot be designed away while AIOS is private, so the reachability
    is closed instead: every step that carries a standing secret must first run
    the trust guard, which refuses any run whose head repository is not this
    one.

    Pinned per step rather than per job, because a secret is scoped to the step
    that declares it; and pinned as the *first* command, so the guard cannot be
    demoted below the use of the credential.
    """
    offenders: list[tuple[str, str, str | None, str]] = []
    for path, job_id, _job, step in _secret_bearing_steps():
        first = _first_command(step.get("run", ""))
        if first != TRUST_GUARD:
            offenders.append((path.name, job_id, step.get("name"), first))
            continue
        if "GITHUB_TOKEN" not in _secret_names(step.get("env", {})):
            offenders.append(
                (path.name, job_id, step.get("name"), "guard has no GITHUB_TOKEN")
            )
    assert not offenders, (
        "steps receive a repository secret without first proving the checked-out "
        f"code was authored here: {offenders}"
    )


def test_a_refused_guard_actually_stops_the_step() -> None:
    """Running the guard first only protects anything if refusing ends the step.

    The order check above silently assumed that. The assumption is false under
    `pwsh`, where a native command's non-zero exit is not a terminating error:
    the guard refuses, the next line fetches the artifact with the token
    anyway, and the step exits with the *last* command's status — green. So the
    shell is pinned too, and pinned explicitly rather than inherited: an
    inherited shell depends on `runs-on`, and moving a job to another runner
    would change the abort semantics without touching the step.
    """
    offenders: list[tuple[str, str, str | None, object]] = []
    for path, job_id, job, step in _secret_bearing_steps():
        shell = step.get("shell")
        if shell in ABORTING_SHELLS:
            continue
        inherited = ((job.get("defaults") or {}).get("run") or {}).get("shell")
        offenders.append(
            (path.name, job_id, step.get("name"), shell or f"inherited:{inherited}")
        )
    assert not offenders, (
        "steps hand over a repository secret under a shell that does not abort on the "
        f"guard's refusal; declare `shell: bash`: {offenders}"
    )


def test_repository_secrets_are_never_scoped_wider_than_a_single_step() -> None:
    """A workflow- or job-level secret would leak past the guarded step.

    The guard protects the step that declares the credential. Declaring one at
    workflow or job level would put it in the environment of every step in
    scope, including steps the guard does not precede, which would make the
    per-step proof above meaningless without failing it.
    """
    for path in _workflow_paths():
        workflow = _workflow(path)
        assert not _secret_names(workflow.get("env", {})) - EPHEMERAL_SECRETS, path.name
        for job_id, job in (workflow.get("jobs") or {}).items():
            leaked = _secret_names(job.get("env", {})) - EPHEMERAL_SECRETS
            assert not leaked, (path.name, job_id, leaked)
            # A job that calls a reusable workflow has no steps to guard, so a
            # secret passed there escapes every check above.
            if job.get("uses"):
                passed = job.get("secrets")
                assert passed != "inherit", (path.name, job_id, "secrets: inherit")
                assert not _secret_names(passed or {}) - EPHEMERAL_SECRETS, (
                    path.name,
                    job_id,
                )


def test_the_trust_guard_exists_where_the_workflows_expect_it() -> None:
    assert (ROOT / "scripts/assert_trusted_head_repository.py").is_file()


def test_the_secret_invariants_see_every_workflow_in_the_directory() -> None:
    """The sweep is the filesystem's, not a constant's."""
    assert {path.name for path in _workflow_paths()} >= {
        CI_WORKFLOW.name,
        BOUNDARY_WORKFLOW.name,
    }
    assert len(_workflow_paths()) == len(
        list((ROOT / ".github/workflows").glob("*.y*ml"))
    )


def test_every_required_context_has_a_deliberate_failure_canary() -> None:
    jobs = _all_jobs()
    for job_id in CANARY_LABELS:
        job = jobs[job_id]
        (canary,) = [
            step
            for step in job["steps"]
            if step.get("name") == "Deliberate failure canary"
        ]
        assert CANARY_LABELS[job_id] in canary["if"]
        assert canary["run"].strip() == "exit 1"


def test_all_actions_are_immutable_sha_pinned() -> None:
    for job in _all_jobs().values():
        for step in job["steps"]:
            if uses := step.get("uses"):
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses), uses


def test_final_gate_is_fail_closed_for_every_upstream_result() -> None:
    final_gate = _workflow(CI_WORKFLOW)["jobs"]["final-gate"]
    required = {
        "quality-gates",
        "manifest-gate",
        "coverage-gate",
        "windows-quality-gates",
        "security-gates",
        "build-gates",
    }

    assert final_gate["if"] == "always()"
    assert set(final_gate["needs"]) == required

    (assertion_step,) = [
        step
        for step in final_gate["steps"]
        if step.get("name") == "Assert required checks"
    ]
    script = assertion_step["run"]
    for job_id in required:
        assert f'${{{{ needs.{job_id}.result }}}}" != "success"' in script

    def accepted(results: dict[str, str]) -> bool:
        return all(results[job_id] == "success" for job_id in required)

    success = {job_id: "success" for job_id in required}
    assert accepted(success)
    for job_id in required:
        for negative_result in ("failure", "cancelled", "skipped"):
            results = success | {job_id: negative_result}
            assert not accepted(results), (job_id, negative_result)
