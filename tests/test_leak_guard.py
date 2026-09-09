"""scripts/ci/prepush/leak_guard.sh (VOYN-OPS-PUBLIC-REPO-CLAUDE-MD-LEAK):
the real script against real throwaway git repos. The guard's contract is
deterministic: block agent-instruction files by name at any depth, block
ADDED lines carrying absolute home paths, never flag pre-existing content,
and a bypass is printed rather than silent.

The two exemptions of
VOYN-W0-AICC-LEAK-GUARD-BLOCKS-FILES-THAT-ALREADY-CARRY-HOME-PATHS are
covered here too -- an added path the BASE version of the same file already
carries, and a file the shipped allowlist covers -- together with what must
keep failing: a genuinely new path, a personal home root anywhere, and an
allowlist entry a branch grants itself.

The home-path literals are assembled from halves everywhere in this file:
the guard scans its own test (no whole-file exclusions -- finding on
f4616fd), so a written-out literal here would refuse every push."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

# Assembled from halves, like the guard's own patterns.
USER_HOME = "/Use" + "rs/someone"
WORK_HOME = "/home/voyn" + "admin"

PREPUSH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ci" / "prepush"
SCRIPT = PREPUSH / "leak_guard.sh"
ALLOWLIST = PREPUSH / "leak_guard_allowlist"


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path):
    """A repo with the guard installed at its canonical path, one base commit
    on main, and a feature branch checked out — the pre-push shape."""
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-b", "main", str(work)], check=True, capture_output=True
    )
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    target = work / "scripts" / "ci" / "prepush" / "leak_guard.sh"
    target.parent.mkdir(parents=True)
    target.write_text(SCRIPT.read_text())
    target.chmod(0o755)
    # The SHIPPED allowlist, at its canonical path and in the base commit --
    # the guard reads it from the base, so the tests exercise the real
    # entries rather than a fixture's idea of them.
    (target.parent / "leak_guard_allowlist").write_text(ALLOWLIST.read_text())
    (work / "base.txt").write_text("clean\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    _git(work, "checkout", "-q", "-b", "feature")
    return work


def _guard(work, env_extra=None):
    import os

    env = dict(os.environ, VOYN_LEAK_GUARD_BASE="main")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "scripts/ci/prepush/leak_guard.sh"],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_clean_commit_passes(repo):
    (repo / "ok.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "ok")
    r = _guard(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "LEAK_GUARD: pass" in r.stdout


@pytest.mark.parametrize("name", ["CLAUDE.md", "nested/dir/CLAUDE.local.md"])
def test_agent_instruction_file_is_refused_at_any_depth(repo, name):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("instructions\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "leak")
    r = _guard(repo)
    assert r.returncode == 1
    assert "agent-instruction file" in r.stdout
    assert name in r.stdout


def test_added_absolute_home_path_is_refused(repo):
    (repo / "doc.md").write_text(f"see {USER_HOME}/Projects/x for details\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "leak")
    r = _guard(repo)
    assert r.returncode == 1
    assert "absolute home paths" in r.stdout


def test_staged_leak_is_caught_before_commit(repo):
    (repo / "CLAUDE.md").write_text("instructions\n")
    _git(repo, "add", "CLAUDE.md")
    r = _guard(repo)
    assert r.returncode == 1
    assert "agent-instruction file 'CLAUDE.md'" in r.stdout


def test_preexisting_home_path_lines_do_not_flag_adjacent_edits(repo):
    """Tracked files already carry historical home-path examples; the guard
    scans ADDED lines only, so editing next to one must stay green."""
    doc = repo / "doc.md"
    _git(repo, "checkout", "-q", "main")
    doc.write_text(f"historical {USER_HOME}/example/path\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "historical")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "main")
    doc.write_text(f"historical {USER_HOME}/example/path\na clean new line\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "adjacent edit")
    r = _guard(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_bypass_is_printed_never_silent(repo):
    (repo / "CLAUDE.md").write_text("instructions\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "leak")
    r = _guard(repo, {"VOYN_LEAK_GUARD": "off"})
    assert r.returncode == 0
    assert "bypassed" in r.stdout


def test_unresolvable_base_fails_closed(repo):
    """Verification finding 1 on f24d081: an unresolvable base used to skip
    the committed-range scans silently — a committed leak reached 'pass'.
    A guard that cannot see the range must refuse."""
    (repo / "CLAUDE.md").write_text("instructions\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "leak")
    r = _guard(repo, {"VOYN_LEAK_GUARD_BASE": "no-such-ref"})
    assert r.returncode == 1
    assert "cannot resolve base" in r.stdout


def test_deleting_a_leaked_instruction_file_is_allowed(repo):
    """Verification finding 2 on f24d081: deletion (the remediation this
    guard exists to force) must not be refused by the by-name check."""
    _git(repo, "checkout", "-q", "main")
    (repo / "CLAUDE.md").write_text("previously leaked\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "historical leak")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "main")
    _git(repo, "rm", "-q", "CLAUDE.md")
    _git(repo, "commit", "-m", "remediate: remove leaked file")
    r = _guard(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_renaming_to_an_instruction_file_is_refused(repo):
    (repo / "notes.md").write_text("instructions\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "notes")
    _git(repo, "mv", "notes.md", "CLAUDE.md")
    _git(repo, "commit", "-m", "rename to instruction file")
    r = _guard(repo)
    assert r.returncode == 1
    assert "agent-instruction file" in r.stdout


def test_trusted_copy_scans_a_target_repo_passed_as_argument(repo, tmp_path):
    """Finding 3 on f24d081 (publish-side wiring): the trusted script copy
    accepts the repository to scan as $1 — the publish gate executes the
    WORKER's copy against the candidate tree, never the candidate's copy."""
    import subprocess

    target = tmp_path / "candidate"
    subprocess.run(["git", "init", "-b", "main", str(target)],
                   check=True, capture_output=True)
    _git(target, "config", "user.email", "t@t")
    _git(target, "config", "user.name", "t")
    (target / "base.txt").write_text("x\n")
    _git(target, "add", ".")
    _git(target, "commit", "-m", "base")
    _git(target, "checkout", "-q", "-b", "feature")
    (target / "CLAUDE.md").write_text("leak\n")
    _git(target, "add", ".")
    _git(target, "commit", "-m", "leak")

    import os
    env = dict(os.environ, VOYN_LEAK_GUARD_BASE="main")
    r = subprocess.run(
        ["bash", str(SCRIPT), str(target)],
        capture_output=True, text=True, check=False, env=env,
    )
    assert r.returncode == 1
    assert "agent-instruction file 'CLAUDE.md'" in r.stdout


# --- exemptions (VOYN-W0-AICC-LEAK-GUARD-BLOCKS-FILES-THAT-ALREADY-CARRY-
# HOME-PATHS): the guard has to stay strict for real leaks while letting the
# fleet edit the files that publish a deployment path by design.


def _on_main(repo, path, text):
    """Put a file on main (the guard's base) and merge it into feature."""
    target = repo / path
    _git(repo, "checkout", "-q", "main")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"base: {path}")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "main")
    return target


def _commit(repo, message="edit"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def test_path_already_in_the_base_version_of_the_same_file_is_allowed(repo):
    """Rule A. deploy/install-agent-principal-isolation.sh and friends carry
    the deployment path; re-adding a line with a path the base version of
    that same file already publishes discloses nothing new."""
    unit = _on_main(
        repo,
        "deploy/install.sh",
        f"REPO={WORK_HOME}/Projects/ai-command-center\n",
    )
    unit.write_text(
        f"REPO={WORK_HOME}/Projects/ai-command-center\n"
        f"mkdir -p {WORK_HOME}/Projects/ai-command-center\n"
    )
    _commit(repo)
    r = _guard(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_new_path_in_a_file_that_already_carries_one_is_still_refused(repo):
    """Rule A is per-OCCURRENCE, not per file: a file carrying one deployment
    path does not become a hole for every other absolute path."""
    unit = _on_main(
        repo,
        "deploy/install.sh",
        f"REPO={WORK_HOME}/Projects/ai-command-center\n",
    )
    unit.write_text(
        f"REPO={WORK_HOME}/Projects/ai-command-center\n"
        f"BACKUP={WORK_HOME}/private/notes\n"
    )
    _commit(repo)
    r = _guard(repo)
    assert r.returncode == 1
    assert "absolute home paths" in r.stdout
    assert "deploy/install.sh" in r.stdout


def test_personal_home_path_added_beside_a_carried_one_is_refused(repo):
    """A line is refused unless EVERY occurrence on it is exempt."""
    unit = _on_main(
        repo,
        "deploy/install.sh",
        f"REPO={WORK_HOME}/Projects/ai-command-center\n",
    )
    unit.write_text(
        f"REPO={WORK_HOME}/Projects/ai-command-center\n"
        f"cp {WORK_HOME}/Projects/ai-command-center/x {USER_HOME}/Desktop/x\n"
    )
    _commit(repo)
    r = _guard(repo)
    assert r.returncode == 1
    assert "absolute home paths" in r.stdout


@pytest.mark.parametrize(
    "path",
    [
        "ops/ci/installer-integration/run.sh",
        "deploy/systemd/voyn-example.service",
    ],
)
def test_allowlisted_fixture_and_unit_accept_a_new_deployment_path(repo, path):
    """Rule B, against the SHIPPED allowlist. The installer-integration
    fixture (`git init -q -b main <deployment path>`) and the systemd units
    are the files the 2026-09-09 incident could not edit; a path that is new
    to the file must be accepted there."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"git init -q -b main {WORK_HOME}/Projects/ai-command-center\n")
    _commit(repo)
    r = _guard(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_allowlist_never_covers_personal_home_paths(repo):
    """WORKER_HOME is the only token; a personal machine path is refused even
    inside an allowlisted file."""
    target = repo / "ops" / "ci" / "installer-integration" / "run.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"REPO={USER_HOME}/Projects/ai-command-center\n")
    _commit(repo)
    r = _guard(repo)
    assert r.returncode == 1
    assert "absolute home paths" in r.stdout


def test_a_branch_cannot_allowlist_itself(repo):
    """The allowlist is read from the BASE commit: an entry a branch grants
    itself is not live until it has landed on the base branch."""
    allowlist = repo / "scripts" / "ci" / "prepush" / "leak_guard_allowlist"
    allowlist.write_text(allowlist.read_text() + "tools/** WORKER_HOME\n")
    (repo / "tools").mkdir()
    (repo / "tools" / "x.sh").write_text(f"REPO={WORK_HOME}/Projects/x\n")
    _commit(repo)
    r = _guard(repo)
    assert r.returncode == 1
    assert "tools/x.sh" in r.stdout


def test_unknown_allowlist_token_fails_closed(repo):
    """An entry the guard does not implement must refuse, not be ignored:
    silently dropping it would leave an operator believing it is live."""
    _on_main(
        repo,
        "scripts/ci/prepush/leak_guard_allowlist",
        ALLOWLIST.read_text() + "ops/ci/** EVERY_HOME\n",
    )
    (repo / "ok.py").write_text("x = 1\n")
    _commit(repo)
    r = _guard(repo)
    assert r.returncode == 1
    assert "unknown" in r.stdout
    assert "EVERY_HOME" in r.stdout


def test_malformed_allowlist_entry_fails_closed(repo):
    _on_main(
        repo,
        "scripts/ci/prepush/leak_guard_allowlist",
        ALLOWLIST.read_text() + "ops/ci/**\n",
    )
    (repo / "ok.py").write_text("x = 1\n")
    _commit(repo)
    r = _guard(repo)
    assert r.returncode == 1
    assert "malformed" in r.stdout


def test_new_file_at_an_allowlisted_path_still_refuses_instruction_files(repo):
    """The allowlist covers check 2 only; the by-name check is absolute."""
    target = repo / "ops" / "ci" / "CLAUDE.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("instructions\n")
    _commit(repo)
    r = _guard(repo)
    assert r.returncode == 1
    assert "agent-instruction file" in r.stdout


def test_shipped_allowlist_covers_the_files_that_carry_paths_by_design():
    """The entries the incident record names, asserted on the real file so a
    silent narrowing of the allowlist shows up here."""
    entries = [
        line.split()
        for line in ALLOWLIST.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert ["ops/ci/**", "WORKER_HOME"] in entries
    assert ["deploy/systemd/**", "WORKER_HOME"] in entries
    assert all(token == "WORKER_HOME" for _, token in entries)


def test_mnemonic_prefixes_do_not_break_the_staged_file_lookup(repo):
    """`git diff --cached` under diff.mnemonicPrefix labels the added side
    `i/<path>`, and diff.noprefix drops the prefix entirely. The guard pins
    a/ and b/ so the `+++` header stays the file's real path: otherwise a
    carried path would be attributed to a file that does not exist and
    refused (rule A can only ask about a file it can name)."""
    _git(repo, "config", "diff.mnemonicPrefix", "true")
    carrier = _on_main(repo, "deploy/install.sh", f"REPO={WORK_HOME}/Projects/aicc\n")
    carrier.write_text(f"REPO={WORK_HOME}/Projects/aicc\ncd {WORK_HOME}/Projects/aicc\n")
    _git(repo, "add", "-A")  # staged, not committed: the --cached scan
    r = _guard(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_path_starting_with_the_diff_prefix_is_not_mistruncated(repo):
    """Pinning also disambiguates the header for a file that really lives
    under `b/`: with diff.noprefix the header would read `+++ b/thing.sh`
    and a naive prefix strip would look the base up under `thing.sh`."""
    _git(repo, "config", "diff.noprefix", "true")
    carrier = _on_main(repo, "b/install.sh", f"REPO={WORK_HOME}/Projects/aicc\n")
    carrier.write_text(f"REPO={WORK_HOME}/Projects/aicc\ncd {WORK_HOME}/Projects/aicc\n")
    _commit(repo, "carried path re-added")
    r = _guard(repo)
    assert r.returncode == 0, r.stdout + r.stderr

    (repo / "b" / "leak.py").write_text(f'P = "{USER_HOME}/Desktop"\n')
    _commit(repo, "leak")
    r = _guard(repo)
    assert r.returncode == 1
    assert "leak.py" in r.stdout
