"""ops/lease_reap.sh (VOYN-W0-AICC-REAPER-NOT-RUNNING) -- the real script,
run by bash against real throwaway trees and a `voyn-lease` stub that
enforces the one thing the real tool enforces about a working directory: it
must resolve to a repository on a branch.

The contract under test is recovery, not the happy path. Independent review
rejected the first revision precisely because its tests covered clean
creation and reuse only, so a bootstrap that could strand itself forever in
an unborn repository passed them. Every state that bootstrap can be
interrupted into therefore gets a test here: unborn HEAD with and without
the marker, detached HEAD, a deleted branch, a `.git` that is gone, and the
directory this script must refuse to touch instead of recovering.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "lease_reap.sh"

# The stub records one JSON line per invocation, including the branch it saw,
# so a test can assert the sweep really did run from a valid repository and
# not merely that the script exited 0.
LEASE_STUB = r"""#!/bin/bash
set -uo pipefail
verb=$1
branch=$(git symbolic-ref --quiet HEAD 2>/dev/null) || branch=""
commit=$(git rev-parse --verify --quiet HEAD 2>/dev/null) || commit=""
python3 - "$verb" "$PWD" "$branch" "$commit" "$@" <<'PY' >>"$AICC_TEST_CALLS"
import json, sys
verb, cwd, branch, commit, *argv = sys.argv[1:]
print(json.dumps({"verb": verb, "cwd": cwd, "branch": branch,
                  "commit": commit, "argv": argv}))
PY
if [ -z "$branch" ] || [ -z "$commit" ]; then
  # What the real tool does with a cwd it cannot resolve to a branch, and
  # the permanent failure this task exists to stop reproducing.
  echo "VOYN_LEASE_REFUSED invalid branch" >&2
  exit 3
fi
case "$verb" in
  list) cat "$AICC_TEST_ROWS" ;;
  acquire) if [ -n "${AICC_TEST_ACQUIRE_FAILS:-}" ]; then
             echo "VOYN_LEASE_REFUSED active owner=someone" >&2; exit 4
           fi ;;
  release) ;;
esac
exit 0
"""


class Harness:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "aicc-preprod"
        self.root.mkdir()
        self.repo = self.root / "lease-reaper-repo"
        self.log = self.root / "lease_reap.log"
        self.calls = tmp_path / "calls.jsonl"
        self.rows = tmp_path / "rows.json"
        self.rows.write_text("[]")
        tool = tmp_path / "voyn-lease-stub"
        tool.write_text(LEASE_STUB)
        tool.chmod(0o755)
        self.tool = tool

    def set_rows(self, rows: list[dict]) -> None:
        self.rows.write_text(json.dumps(rows))

    def run(self, **env_extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            AICC_LEASE_REAP_REPO=str(self.repo),
            AICC_LEASE_REAP_LOG=str(self.log),
            VOYN_LEASE_TOOL=str(self.tool),
            AICC_TEST_CALLS=str(self.calls),
            AICC_TEST_ROWS=str(self.rows),
        )
        env.update(env_extra)
        return subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, check=False, env=env
        )

    def run_without_path_overrides(
        self, **env_extra: str
    ) -> subprocess.CompletedProcess[str]:
        """Drive the script the way the legacy hand-installed cron entry does:
        no AICC_LEASE_REAP_* at all, so the defaults themselves are exercised."""
        env = dict(
            os.environ,
            VOYN_LEASE_TOOL=str(self.tool),
            AICC_TEST_CALLS=str(self.calls),
            AICC_TEST_ROWS=str(self.rows),
        )
        env.pop("AICC_LEASE_REAP_REPO", None)
        env.pop("AICC_LEASE_REAP_LOG", None)
        env.pop("AICC_LEASE_REAP_ROOT", None)
        env.update(env_extra)
        return subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, check=False, env=env
        )

    @property
    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def call_records(self) -> list[dict]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]


@pytest.fixture
def reaper(tmp_path):
    return Harness(tmp_path)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _head_branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def _row(repository_id: str, expires_at: str) -> dict:
    return {"repository_id": repository_id, "expires_at": expires_at}


PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


# --- bootstrap: creation, reuse, and every interrupted state -------------


def test_creates_identity_repository_when_absent(reaper):
    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert _head_branch(reaper.repo) == "refs/heads/lease-reaper"
    assert (reaper.repo / ".aicc-lease-reaper-identity").exists()
    assert "ready on branch lease-reaper" in reaper.log_text
    assert [c["branch"] for c in reaper.call_records()] == ["refs/heads/lease-reaper"]


def test_valid_identity_repository_is_reused_untouched(reaper):
    reaper.run()
    first = _git(reaper.repo, "rev-parse", "HEAD").stdout.strip()

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert _git(reaper.repo, "rev-parse", "HEAD").stdout.strip() == first
    assert reaper.log_text.count("rebuilding") == 0


def test_unborn_identity_repository_is_rebuilt(reaper):
    """The rejected revision's defect: `git init` ran, the commit never did,
    and every later run skipped initialization because `.git` existed."""
    reaper.repo.mkdir()
    _git(reaper.repo, "init", "-q")
    (reaper.repo / ".aicc-lease-reaper-identity").write_text("marker\n")

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert _head_branch(reaper.repo) == "refs/heads/lease-reaper"
    assert "rebuilding" in reaper.log_text
    assert [c["branch"] for c in reaper.call_records()] == ["refs/heads/lease-reaper"]


def test_unborn_identity_repository_without_marker_is_rebuilt(reaper):
    """The upgrade path off the rejected revision: its half-built repository
    carries no marker, and refusing it would strand the reaper forever."""
    reaper.repo.mkdir()
    _git(reaper.repo, "init", "-q")

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert _head_branch(reaper.repo) == "refs/heads/lease-reaper"
    assert (reaper.repo / ".aicc-lease-reaper-identity").exists()


def test_detached_head_identity_repository_is_repaired(reaper):
    reaper.run()
    _git(reaper.repo, "checkout", "--detach", "HEAD")
    assert _head_branch(reaper.repo) == ""

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert _head_branch(reaper.repo) == "refs/heads/lease-reaper"
    assert [c["branch"] for c in reaper.call_records()][-1] == "refs/heads/lease-reaper"


def test_identity_repository_with_deleted_branch_is_repaired(reaper):
    reaper.run()
    (reaper.repo / ".git" / "refs" / "heads" / "lease-reaper").unlink()
    packed = reaper.repo / ".git" / "packed-refs"
    if packed.exists():
        packed.unlink()

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert _head_branch(reaper.repo) == "refs/heads/lease-reaper"
    assert _git(reaper.repo, "rev-parse", "--verify", "HEAD").returncode == 0


def test_identity_repository_that_is_no_longer_a_repository_is_rebuilt(reaper):
    reaper.run()
    subprocess.run(["rm", "-rf", str(reaper.repo / ".git")], check=True)

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert _head_branch(reaper.repo) == "refs/heads/lease-reaper"


def test_unrecognised_directory_is_refused_not_destroyed(reaper):
    reaper.repo.mkdir()
    precious = reaper.repo / "someones-work.txt"
    precious.write_text("not the reaper's\n")

    result = reaper.run()

    assert result.returncode == 1
    assert "refusing to replace a directory this script does not own" in reaper.log_text
    assert precious.read_text() == "not the reaper's\n"
    assert reaper.call_records() == []


def test_existing_valid_repository_without_marker_is_used_as_is(reaper):
    """A $REPO an operator points at a real clone is a working directory, not
    something to replace: it is already what the tool needs."""
    reaper.repo.mkdir()
    _git(reaper.repo, "init", "-q", "-b", "main")
    _git(reaper.repo, "config", "user.email", "t@t")
    _git(reaper.repo, "config", "user.name", "t")
    (reaper.repo / "work.txt").write_text("real\n")
    _git(reaper.repo, "add", ".")
    _git(reaper.repo, "commit", "-q", "-m", "base")

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert _head_branch(reaper.repo) == "refs/heads/main"
    assert (reaper.repo / "work.txt").read_text() == "real\n"
    assert not (reaper.repo / ".aicc-lease-reaper-identity").exists()


def test_rebuild_leaves_no_scratch_tree_behind(reaper):
    reaper.run()

    leftovers = [p.name for p in reaper.root.iterdir() if ".lease-reaper-init." in p.name]
    replaced = [p.name for p in reaper.root.iterdir() if ".lease-reaper-replaced." in p.name]
    assert leftovers == []
    assert replaced == []


def test_repository_is_validated_on_every_run_not_only_the_first(reaper):
    """Validity is re-derived from disk each tick, so a repository that goes
    bad between ticks recovers on the next one rather than at the next human."""
    reaper.set_rows([_row("proj-a", PAST)])
    reaper.run()
    _git(reaper.repo, "checkout", "--detach", "HEAD")
    reaper.calls.unlink()

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    verbs = [c["verb"] for c in reaper.call_records()]
    assert "acquire" in verbs and "release" in verbs


# --- the sweep itself ----------------------------------------------------


def test_expired_row_is_taken_over_and_released(reaper):
    reaper.set_rows([_row("proj-a", PAST)])

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    calls = {c["verb"]: c for c in reaper.call_records()}
    assert "--auto-takeover" in calls["acquire"]["argv"]
    assert "proj-a" in calls["acquire"]["argv"]
    assert "proj-a" in calls["release"]["argv"]
    acquire_session = calls["acquire"]["argv"][
        calls["acquire"]["argv"].index("--session") + 1
    ]
    release_session = calls["release"]["argv"][
        calls["release"]["argv"].index("--session") + 1
    ]
    assert acquire_session == release_session
    assert "reaped proj-a" in reaper.log_text
    assert "OK: scanned 1 row(s), reaped 1" in reaper.log_text


def test_live_row_is_left_alone(reaper):
    reaper.set_rows([_row("proj-live", FUTURE)])

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert [c["verb"] for c in reaper.call_records()] == ["list"]
    assert "OK: scanned 1 row(s), reaped 0" in reaper.log_text


def test_unparseable_expiry_skips_the_row(reaper):
    reaper.set_rows([_row("proj-bad", "not-a-timestamp"), _row("proj-a", PAST)])

    result = reaper.run()

    assert result.returncode == 0, result.stderr
    assert "could not parse expires_at=not-a-timestamp" in reaper.log_text
    acquired = [c for c in reaper.call_records() if c["verb"] == "acquire"]
    assert [c["argv"][c["argv"].index("--repository") + 1] for c in acquired] == ["proj-a"]


def test_refused_acquire_does_not_stop_the_sweep(reaper):
    reaper.set_rows([_row("proj-a", PAST), _row("proj-b", PAST)])

    result = reaper.run(AICC_TEST_ACQUIRE_FAILS="1")

    assert result.returncode == 0, result.stderr
    assert "acquire failed for proj-a" in reaper.log_text
    assert "acquire failed for proj-b" in reaper.log_text
    assert "OK: scanned 2 row(s), reaped 0" in reaper.log_text


def test_non_array_listing_is_fatal(reaper):
    reaper.rows.write_text('{"error": "nope"}')

    result = reaper.run()

    assert result.returncode == 1
    assert "did not return a JSON array" in reaper.log_text


def _path_with_only(tmp_path: Path, names: list[str]) -> str:
    """A PATH carrying exactly `names`, so a test can take one tool away."""
    stub_bin = tmp_path / "trimmed-bin"
    stub_bin.mkdir(exist_ok=True)
    for name in names:
        found = shutil.which(name)
        if found:
            (stub_bin / name).symlink_to(found)
    return str(stub_bin)


_BASE_TOOLS = [
    "bash", "git", "date", "hostname", "mktemp", "ls", "find", "rm", "mv",
    "cat", "dirname", "mkdir", "python3", "jq",
]


def test_missing_jq_is_fatal_and_logged(reaper, tmp_path):
    trimmed = [t for t in _BASE_TOOLS if t != "jq"]

    result = reaper.run(PATH=_path_with_only(tmp_path, trimmed))

    assert result.returncode == 1
    assert "jq not found on PATH" in reaper.log_text


def test_failed_initialization_leaves_nothing_behind_and_recovers(reaper, tmp_path):
    """The other half of the review finding: initialization must be atomic,
    not merely re-checked. A commit that dies mid-bootstrap must publish no
    repository at all -- $REPO is never the place a half-built one appears --
    and the next tick must still succeed on its own."""
    sabotage = tmp_path / "sabotage-bin"
    sabotage.mkdir()
    (sabotage / "git").write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "commit" ]; then echo "simulated crash" >&2; exit 1; fi\n'
        "done\n"
        f'exec {shutil.which("git")} "$@"\n'
    )
    (sabotage / "git").chmod(0o755)
    broken_path = f"{sabotage}:{_path_with_only(tmp_path, _BASE_TOOLS)}"

    failed = reaper.run(PATH=broken_path)

    assert failed.returncode != 0
    assert not reaper.repo.exists()
    assert [p.name for p in reaper.root.iterdir() if p.name.startswith(".lease-reaper")] == []
    assert reaper.call_records() == []

    recovered = reaper.run()

    assert recovered.returncode == 0, recovered.stderr
    assert _head_branch(reaper.repo) == "refs/heads/lease-reaper"


def test_failed_rebuild_does_not_destroy_the_previous_repository(reaper, tmp_path):
    """A rebuild that cannot finish must leave the old tree exactly where it
    was: the displaced copy is only removed after the new one is in place."""
    reaper.run()
    marker = reaper.repo / ".aicc-lease-reaper-identity"
    subprocess.run(["rm", "-rf", str(reaper.repo / ".git")], check=True)
    sabotage = tmp_path / "sabotage-bin"
    sabotage.mkdir()
    (sabotage / "git").write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "commit" ]; then echo "simulated crash" >&2; exit 1; fi\n'
        "done\n"
        f'exec {shutil.which("git")} "$@"\n'
    )
    (sabotage / "git").chmod(0o755)

    failed = reaper.run(PATH=f"{sabotage}:{_path_with_only(tmp_path, _BASE_TOOLS)}")

    assert failed.returncode != 0
    assert marker.exists()
    assert [p.name for p in reaper.root.iterdir() if ".lease-reaper-replaced." in p.name] == []


def test_default_paths_follow_the_invoking_home(reaper, tmp_path):
    """No AICC_LEASE_REAP_* set: the legacy cron invocation must still land in
    the operator's own preprod tree, with no absolute path baked into a public
    repository (VOYN-OPS-PUBLIC-REPO-CLAUDE-MD-LEAK)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = reaper.run_without_path_overrides(HOME=str(fake_home))

    assert result.returncode == 0, result.stderr
    assert _head_branch(fake_home / "aicc-preprod" / "lease-reaper-repo") == (
        "refs/heads/lease-reaper"
    )
    assert (fake_home / "aicc-preprod" / "lease_reap.log").exists()


def test_no_home_and_no_override_refuses_instead_of_guessing(reaper, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    env.update(
        VOYN_LEASE_TOOL=str(reaper.tool),
        AICC_TEST_CALLS=str(reaper.calls),
        AICC_TEST_ROWS=str(reaper.rows),
    )
    env.pop("AICC_LEASE_REAP_REPO", None)
    env.pop("AICC_LEASE_REAP_LOG", None)

    result = subprocess.run(
        ["bash", "-c", f'unset HOME; exec bash {SCRIPT}'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 1
    assert "nowhere to put the identity repository" in result.stderr
    assert reaper.call_records() == []


# --- the unit that makes it run at all -----------------------------------


UNIT_DIR = Path(__file__).resolve().parents[2] / "deploy" / "systemd"


def test_reaper_has_a_versioned_unit_that_runs_the_script():
    """"Not running" was the whole defect: the sweep existed only as a script
    plus a claim about one hand-configured host."""
    service = (UNIT_DIR / "voyn-aicc-lease-reap.service").read_text()

    assert "ExecStart=" in service
    exec_start = next(
        line for line in service.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_start.endswith("ops/lease_reap.sh")
    # The versioned copy inside the immutable release, not one inside a
    # checkout ordinary work can edit out from under the fleet.
    assert exec_start.startswith("ExecStart=/opt/aicc/")
    assert "Type=oneshot" in service


def test_reaper_timer_is_installable_and_bounded_by_the_promised_five_minutes():
    timer = (UNIT_DIR / "voyn-aicc-lease-reap.timer").read_text()

    assert "OnUnitActiveSec=5min" in timer
    assert "OnBootSec=" in timer
    assert "WantedBy=timers.target" in timer


def test_unit_environment_names_match_the_script_overrides():
    """A renamed override in one file and not the other silently puts the
    reaper back on a path nothing owns."""
    service = (UNIT_DIR / "voyn-aicc-lease-reap.service").read_text()
    script = SCRIPT.read_text()

    for name in ("AICC_LEASE_REAP_REPO", "AICC_LEASE_REAP_LOG"):
        assert f"Environment={name}=" in service
        assert f"{name}:-" in script
