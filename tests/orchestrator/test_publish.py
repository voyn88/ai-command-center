"""publish_run (BO-S3b part 1): the git/lease/gh mechanics against a real
local git repo, with the lease tool and gh faked by tiny scripts on PATH.
No network, no GitHub — the contract is the argv and the branch state."""

from __future__ import annotations

import subprocess

import pytest

from command_center.orchestrator.publish import PublishConfig, publish_run


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path):
    """A clone with an 'origin' bare remote, one base commit, and a fake
    lease tool + gh on PATH that record their calls."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "base.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "origin", "main")

    bin_ = tmp_path / "bin"
    bin_.mkdir()
    calls = tmp_path / "calls.log"
    lease = bin_ / "voyn-lease"
    lease.write_text(f"#!/bin/sh\necho \"lease $*\" >> {calls}\nexit 0\n")
    lease.chmod(0o755)
    gh = bin_ / "gh"
    gh.write_text(
        f"#!/bin/sh\necho \"gh $*\" >> {calls}\n"
        "case \"$2\" in\n"
        "  view) exit 1 ;;\n"  # no existing PR
        "  create) echo 'https://github.com/x/y/pull/1'; exit 0 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    return work, bin_, calls


def _cfg(bin_):
    return PublishConfig(
        lease_tool=str(bin_ / "voyn-lease"), repository="ai-command-center",
        owner="server-worker", session="s1", task="VOYN-W0-TEST",
        deploy_key="/dev/null",
    )


def _with_path(bin_, monkeypatch):
    import os
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")


def test_a_run_with_no_commit_is_nothing_to_publish(repo, monkeypatch):
    work, bin_, _ = repo
    _with_path(bin_, monkeypatch)
    r = publish_run(work, _cfg(bin_))
    assert not r.ok and r.reason == "nothing_to_publish"


def test_dirty_tree_at_base_is_operational_failure_not_nothing_to_publish(
    repo, monkeypatch
):
    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    (work / "uncommitted.txt").write_text("only surviving agent diff\n")

    r = publish_run(work, _cfg(bin_))

    assert not r.ok and r.reason == "uncommitted_changes"
    assert (work / "uncommitted.txt").exists()
    assert not calls.exists(), "publisher must not acquire a push lease for dirty state"


def test_a_commit_is_pushed_under_the_lease_and_a_pr_opens(repo, monkeypatch):
    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    (work / "change.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "work")
    r = publish_run(work, _cfg(bin_))
    assert r.ok, r.reason
    assert r.branch == "backlog/VOYN-W0-TEST"
    assert r.pr_url == "https://github.com/x/y/pull/1"
    assert r.head_sha
    # branch landed on the remote
    out = subprocess.run(["git", "ls-remote", "--heads", str(work.parent / "origin.git")],
                         capture_output=True, text=True, check=False).stdout
    assert "backlog/VOYN-W0-TEST" in out
    log = calls.read_text()
    assert " acquire " in log and " release " in log  # lease taken and freed
    assert " install-hooks " in log  # hook's on-disk identity kept fresh
    assert log.index("acquire") < log.index("install-hooks") < log.index("create")
    # VOYN-W0-AICC-LEASE-TTL-CONTRACT-BROKEN: --ttl was never forwarded to
    # the CLI at all, so acquire silently got the tool's own default
    # instead of PublishConfig.ttl's declared 600.
    assert "--ttl 600" in log


def test_already_durable_branch_is_rechecked_before_pr(repo, monkeypatch):
    """A stale pre-agent remote SHA must never authorize a PR for another tip."""
    from dataclasses import replace

    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    base = _git(work, "rev-parse", "HEAD").stdout.strip()
    (work / "candidate.txt").write_text("candidate\n")
    _git(work, "add", "candidate.txt")
    _git(work, "commit", "-m", "candidate")
    candidate = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "push", "origin", "HEAD:refs/heads/backlog/VOYN-W0-TEST")
    (work / "concurrent.txt").write_text("concurrent\n")
    _git(work, "add", "concurrent.txt")
    _git(work, "commit", "-m", "concurrent update")
    _git(work, "push", "origin", "HEAD:refs/heads/backlog/VOYN-W0-TEST")
    _git(work, "checkout", "--detach", candidate)

    result = publish_run(
        work,
        replace(
            _cfg(bin_),
            base_sha=base,
            remote_sha=candidate,
            remote_sha_known=True,
        ),
    )

    assert not result.ok and result.reason == "remote_branch_changed_before_pr"
    assert not calls.exists() or "gh " not in calls.read_text()


def test_release_lease_false_never_calls_release(repo, monkeypatch):
    """VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE, independent-review finding:
    a caller holding the full-lifecycle lease across provision->agent->
    tests->publish must not have it dropped mid-`publish_run` -- `acquire`/
    `install-hooks` stay (idempotent re-affirmation), but `release` is a
    real termination of the row and must be left to the caller's own
    `writer_lease.hold()` exiting, after this function returns."""
    from dataclasses import replace

    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    (work / "change.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "work")

    r = publish_run(work, replace(_cfg(bin_), release_lease=False))
    assert r.ok, r.reason
    log = calls.read_text()
    assert " acquire " in log
    assert " install-hooks " in log
    assert " release " not in log


def test_release_lease_false_still_skips_release_on_install_hooks_failure(repo, monkeypatch):
    """The early-exit path (install-hooks fails) has its own release call --
    it must respect `release_lease` too, not just the happy-path `finally`."""
    from dataclasses import replace

    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    (work / "change.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "work")
    lease = bin_ / "voyn-lease"
    lease.write_text(
        f"#!/bin/sh\necho \"lease $*\" >> {calls}\n"
        "case \"$3\" in\n"  # --repo <path> <verb> ... -- verb is $3
        "  install-hooks) exit 1 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    lease.chmod(0o755)

    r = publish_run(work, replace(_cfg(bin_), release_lease=False))
    assert not r.ok and r.reason.startswith("install_hooks_failed")
    log = calls.read_text()
    assert " acquire " in log
    assert " release " not in log


def test_a_github_ssh_origin_is_pushed_over_https(repo, monkeypatch):
    """VOYN-W0-AICC-DEPLOY-KEY-WRITE-DENIED (2026-08-21): a verified,
    correctly-registered, non-read-only deploy key still had its write
    silently denied by GitHub with no actionable diagnostic (reproduced live
    against a public repo). `gh`'s own credential helper (`gh auth
    setup-git`, already configured host-wide) is what recovered every
    manual push this session, so it is now the primary path: an
    `origin` shaped like `git@github.com:org/repo.git` is rewritten to
    `https://github.com/org/repo.git` and pushed there instead of over the
    SSH deploy key. Proven here by pointing `origin` at a github.com URL
    that would fail DNS/auth if actually dialled, and asserting the push
    argv names that rewritten HTTPS target -- not `origin` -- via a `git`
    shim on PATH that logs argv instead of a real network push."""
    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    _git(work, "remote", "set-url", "origin", "git@github.com:voyn88/ai-command-center.git")
    (work / "change.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "work")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()

    import shutil

    real_git = shutil.which("git")
    git_shim = bin_ / "git"
    git_shim.write_text(
        f"#!/bin/sh\n"
        f"echo \"git $*\" >> {calls}\n"
        "case \"$1\" in\n"
        f"  ls-remote) n=$(grep -c '^git ls-remote' {calls}); "
        f"[ \"$n\" -gt 1 ] && echo \"{head} refs/heads/backlog/VOYN-W0-TEST\"; exit 0 ;;\n"
        "  push) exit 0 ;;\n"
        f"  *) exec {real_git} \"$@\" ;;\n"
        "esac\n"
    )
    git_shim.chmod(0o755)

    r = publish_run(work, _cfg(bin_))
    assert r.ok, r.reason
    log = calls.read_text()
    push_lines = [line for line in log.splitlines() if line.startswith("git push")]
    assert len(push_lines) == 1, log
    assert "https://github.com/voyn88/ai-command-center.git" in push_lines[0]
    assert "git@github.com" not in push_lines[0]
    assert "--force-with-lease=refs/heads/backlog/VOYN-W0-TEST:" in push_lines[0]


def test_a_github_https_update_uses_the_observed_remote_sha(repo, monkeypatch):
    """An URL target has no remote-tracking ref for bare --force-with-lease.
    Protect an update with the exact SHA observed immediately before push."""
    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    _git(work, "remote", "set-url", "origin", "https://github.com/voyn88/ai-command-center.git")
    (work / "change.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "work")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()

    import shutil

    real_git = shutil.which("git")
    expected = "a" * 40
    git_shim = bin_ / "git"
    git_shim.write_text(
        f"#!/bin/sh\n"
        f"echo \"git $*\" >> {calls}\n"
        "case \"$1\" in\n"
        f"  ls-remote) n=$(grep -c '^git ls-remote' {calls}); "
        f"if [ \"$n\" -eq 1 ]; then echo \"{expected} refs/heads/backlog/VOYN-W0-TEST\"; "
        f"else echo \"{head} refs/heads/backlog/VOYN-W0-TEST\"; fi; exit 0 ;;\n"
        "  push) exit 0 ;;\n"
        f"  *) exec {real_git} \"$@\" ;;\n"
        "esac\n"
    )
    git_shim.chmod(0o755)

    r = publish_run(work, _cfg(bin_))
    assert r.ok, r.reason
    log = calls.read_text()
    push_lines = [line for line in log.splitlines() if line.startswith("git push")]
    assert len(push_lines) == 1, log
    assert (
        f"--force-with-lease=refs/heads/backlog/VOYN-W0-TEST:{expected}"
        in push_lines[0]
    )


@pytest.mark.parametrize(
    "ls_remote_action",
    [
        "exit 7",
        (
            "echo 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
            "refs/heads/backlog/VOYN-W0-TEST'; "
            "echo 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb "
            "refs/heads/backlog/VOYN-W0-TEST'; exit 0"
        ),
        (
            "echo 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
            "refs/heads/backlog/WRONG'; exit 0"
        ),
        "echo 'not-a-sha refs/heads/backlog/VOYN-W0-TEST'; exit 0",
    ],
)
def test_github_https_publish_fails_closed_on_untrusted_remote_lookup(
    repo, monkeypatch, ls_remote_action
):
    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    _git(work, "remote", "set-url", "origin", "https://github.com/voyn88/ai-command-center.git")
    (work / "change.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "work")

    import shutil

    real_git = shutil.which("git")
    git_shim = bin_ / "git"
    git_shim.write_text(
        f"#!/bin/sh\n"
        f"echo \"git $*\" >> {calls}\n"
        "case \"$1\" in\n"
        f"  ls-remote) {ls_remote_action} ;;\n"
        "  push) exit 0 ;;\n"
        f"  *) exec {real_git} \"$@\" ;;\n"
        "esac\n"
    )
    git_shim.chmod(0o755)

    r = publish_run(work, _cfg(bin_))
    assert not r.ok
    assert r.reason == "cannot_read_remote_branch_for_force_lease"
    assert not any(line.startswith("git push") for line in calls.read_text().splitlines())


def test_lease_refusal_does_not_push(repo, monkeypatch):
    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    (work / "c.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "w")
    (bin_ / "voyn-lease").write_text(f"#!/bin/sh\necho \"lease $*\" >> {calls}\nexit 3\n")
    (bin_ / "voyn-lease").chmod(0o755)

    r = publish_run(work, _cfg(bin_))
    assert not r.ok and r.reason.startswith("lease_unavailable")
    out = subprocess.run(["git", "ls-remote", "--heads", str(work.parent / "origin.git")],
                         capture_output=True, text=True, check=False).stdout
    assert "backlog" not in out  # never pushed


def test_stale_hook_identity_fails_closed_without_pushing(repo, monkeypatch):
    """VOYN-W0-AICC-LEASE-VERIFY-MISMATCH-BLOCKS-ALL-PUBLISH, live-reproduced
    2026-08-21: `install-hooks` is what re-provisions the pre-push hook's
    on-disk identity file so it matches the lease `acquire` just took --
    without it, the hook's `verify` step (not exercised by these fakes,
    which never actually run the hook) would refuse the push against a
    frozen, wrong identity. If `install-hooks` itself fails here, that must
    fail closed: release the lease and never attempt the push, rather than
    pushing anyway into a `verify` we already know will reject it."""
    work, bin_, calls = repo
    _with_path(bin_, monkeypatch)
    (work / "c.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "w")
    (bin_ / "voyn-lease").write_text(
        f"#!/bin/sh\necho \"lease $*\" >> {calls}\n"
        # argv shape: voyn-lease --repo <path> <verb> ... -- verb is $3.
        "case \"$3\" in\n"
        "  install-hooks) exit 5 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_ / "voyn-lease").chmod(0o755)

    r = publish_run(work, _cfg(bin_))
    assert not r.ok and r.reason.startswith("install_hooks_failed")
    log = calls.read_text()
    assert " acquire " in log and " install-hooks " in log and " release " in log
    out = subprocess.run(["git", "ls-remote", "--heads", str(work.parent / "origin.git")],
                         capture_output=True, text=True, check=False).stdout
    assert "backlog" not in out  # never pushed
