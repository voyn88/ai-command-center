"""Adversarial coverage for the immutable-release content manifest.

The installer selects `/opt/aicc/current -> releases/<sha>` and every worker
unit executes from it. A release directory that merely *exists* with the right
name used to be accepted on a root:root check of its top directory alone;
these tests pin the cases that must instead be refused before selection.

Everything runs as the invoking (unprivileged) user: the module takes the
trusted uid/gid as parameters precisely so its guarantees are testable without
root, and the production caller passes 0/0.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_install_transaction.py"
    spec = importlib.util.spec_from_file_location("aicc_install_transaction", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


UID = os.geteuid()
GID = os.getegid()
RELEASE_ID = "a" * 40


def _trusted(module, path: Path, manifest: Path, release_id: str = RELEASE_ID):
    return module.verify_release_manifest(
        path, manifest, release_id, trusted_uid=UID, trusted_gid=GID
    )


def _record(module, tree: Path, manifest: Path, release_id: str = RELEASE_ID):
    return module.record_release_manifest(
        tree, manifest, release_id, trusted_uid=UID, trusted_gid=GID
    )


@pytest.fixture()
def release(tmp_path):
    tree = tmp_path / "releases" / RELEASE_ID
    (tree / "command_center").mkdir(parents=True)
    (tree / "command_center" / "worker.py").write_text("print('worker')\n")
    (tree / "deploy").mkdir()
    (tree / "deploy" / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (tree / "deploy" / "run.sh").chmod(0o755)
    # The interpreter venv legitimately contains a symlink; it is recorded as a
    # link and its target is never followed.
    (tree / ".venv" / "bin").mkdir(parents=True)
    (tree / ".venv" / "bin" / "python").symlink_to("/usr/bin/python3")
    return tree


def test_freshly_recorded_release_verifies(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    recorded = _record(module, release, manifest)
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert _trusted(module, release, manifest) == recorded
    assert any(entry["kind"] == "symlink" for entry in recorded)


def test_missing_manifest_is_refused_rather_than_rebuilt(release, tmp_path):
    """The unattested pre-existing release is the whole point of the gate."""
    module = _module()
    with pytest.raises(module.ReleaseRefused, match="missing or unsafe"):
        _trusted(module, release, tmp_path / "absent.json")


def test_partial_release_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    (release / "command_center" / "worker.py").unlink()
    with pytest.raises(module.ReleaseRefused, match="incomplete"):
        _trusted(module, release, manifest)


def test_unattested_extra_content_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    (release / "command_center" / "backdoor.py").write_text("import os\n")
    with pytest.raises(module.ReleaseRefused, match="unattested content"):
        _trusted(module, release, manifest)


def test_corrupt_content_under_an_unchanged_name_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    (release / "command_center" / "worker.py").write_text("print('evil')\n")
    with pytest.raises(module.ReleaseRefused, match="does not match manifest"):
        _trusted(module, release, manifest)


def test_file_replaced_by_symlink_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    target = release / "command_center" / "worker.py"
    target.unlink()
    target.symlink_to("/etc/passwd")
    with pytest.raises(module.ReleaseRefused, match="does not match manifest"):
        _trusted(module, release, manifest)


def test_symlink_retargeted_to_attacker_path_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    link = release / ".venv" / "bin" / "python"
    link.unlink()
    link.symlink_to(tmp_path / "attacker-python")
    with pytest.raises(module.ReleaseRefused, match="does not match manifest"):
        _trusted(module, release, manifest)


def test_hardlinked_release_file_is_refused(release, tmp_path):
    """`chmod -R a-w` freezes the release path, not an alias to the inode."""
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    alias = tmp_path / "alias.py"
    os.link(release / "command_center" / "worker.py", alias)
    with pytest.raises(module.ReleaseRefused, match="hardlinked"):
        _trusted(module, release, manifest)


def test_group_or_world_writable_release_file_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    (release / "command_center" / "worker.py").chmod(0o666)
    with pytest.raises(module.ReleaseRefused, match="group/world writable"):
        _trusted(module, release, manifest)


def test_writable_release_cannot_be_recorded_either(release, tmp_path):
    """A faithful manifest of a writable tree must not legitimise that tree."""
    module = _module()
    (release / "deploy").chmod(0o777)
    with pytest.raises(module.ReleaseRefused, match="group/world writable"):
        _record(module, release, tmp_path / "manifest.json")


def test_mode_change_alone_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    (release / "deploy" / "run.sh").chmod(0o700)
    with pytest.raises(module.ReleaseRefused, match="does not match manifest"):
        _trusted(module, release, manifest)


def test_manifest_for_a_different_release_id_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    with pytest.raises(module.ReleaseRefused, match="identity mismatch"):
        _trusted(module, release, manifest, release_id="b" * 40)


def test_non_hex_release_id_is_refused(release, tmp_path):
    module = _module()
    with pytest.raises(module.ReleaseRefused, match="40 lowercase hex"):
        _record(module, release, tmp_path / "manifest.json", release_id="../escape")


def test_tampered_manifest_entries_fail_their_own_content_hash(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    document = json.loads(manifest.read_text())
    for entry in document["entries"]:
        if entry.get("kind") == "file" and entry["path"].endswith("worker.py"):
            entry["sha256"] = "0" * 64
    manifest.write_text(json.dumps(document))
    with pytest.raises(module.ReleaseRefused, match="content hash mismatch"):
        _trusted(module, release, manifest)


def test_malformed_manifest_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{not json")
    manifest.chmod(0o600)
    with pytest.raises(module.ReleaseRefused, match="malformed"):
        _trusted(module, release, manifest)


def test_group_readable_manifest_is_refused(release, tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    _record(module, release, manifest)
    manifest.chmod(0o644)
    with pytest.raises(module.ReleaseRefused, match="mode 0600"):
        _trusted(module, release, manifest)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    ).stdout.strip()


@pytest.fixture()
def committed(tmp_path):
    """A real repository plus a release tree built from its commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    (repo / "command_center").mkdir()
    (repo / "command_center" / "worker.py").write_text("print('worker')\n")
    (repo / "deploy").mkdir()
    (repo / "deploy" / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "deploy" / "run.sh").chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "release")
    sha = _git(repo, "rev-parse", "HEAD")
    tree = tmp_path / "releases" / sha
    tree.mkdir(parents=True)
    subprocess.run(
        f"git -C {repo} archive --format=tar HEAD | tar -xf - -C {tree}",
        shell=True,
        check=True,
    )
    (tree / ".venv").mkdir()
    (tree / ".venv" / "marker").write_text("venv\n")
    return repo, tree, sha


def test_release_built_from_the_commit_verifies_against_git(committed, tmp_path):
    module = _module()
    repo, tree, sha = committed
    manifest = tmp_path / "manifest.json"
    _record(module, tree, manifest, release_id=sha)
    module.verify_release_manifest(
        tree, manifest, sha, repo_root=repo, trusted_uid=UID, trusted_gid=GID
    )


def test_same_name_wrong_tree_release_is_refused_by_git_authority(committed, tmp_path):
    """The attacker rewrites both the release content AND its manifest; the
    committed tree is the independent authority that still catches it."""
    module = _module()
    repo, tree, sha = committed
    (tree / "command_center" / "worker.py").write_text("import os  # implanted\n")
    manifest = tmp_path / "manifest.json"
    _record(module, tree, manifest, release_id=sha)
    # Self-consistent against its own manifest ...
    _trusted(module, tree, manifest, release_id=sha)
    # ... and still refused against the commit it claims to be.
    with pytest.raises(module.ReleaseRefused, match="does not match committed blob"):
        module.verify_release_manifest(
            tree, manifest, sha, repo_root=repo, trusted_uid=UID, trusted_gid=GID
        )


def test_committed_file_absent_from_release_is_refused(committed, tmp_path):
    module = _module()
    repo, tree, sha = committed
    (tree / "deploy" / "run.sh").unlink()
    manifest = tmp_path / "manifest.json"
    _record(module, tree, manifest, release_id=sha)
    with pytest.raises(module.ReleaseRefused, match="missing from release"):
        module.verify_release_manifest(
            tree, manifest, sha, repo_root=repo, trusted_uid=UID, trusted_gid=GID
        )


def test_dropped_executable_bit_is_refused_against_git(committed, tmp_path):
    module = _module()
    repo, tree, sha = committed
    (tree / "deploy" / "run.sh").chmod(0o444)
    manifest = tmp_path / "manifest.json"
    _record(module, tree, manifest, release_id=sha)
    with pytest.raises(module.ReleaseRefused, match="executable bit drifted"):
        module.verify_release_manifest(
            tree, manifest, sha, repo_root=repo, trusted_uid=UID, trusted_gid=GID
        )


def test_read_only_release_still_verifies(committed, tmp_path):
    """Production runs `chmod -R a-w`; verification must not need write access."""
    module = _module()
    repo, tree, sha = committed
    manifest = tmp_path / "manifest.json"
    subprocess.run(["chmod", "-R", "a-w", str(tree)], check=True)
    try:
        _record(module, tree, manifest, release_id=sha)
        module.verify_release_manifest(
            tree, manifest, sha, repo_root=repo, trusted_uid=UID, trusted_gid=GID
        )
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(tree)], check=True)


def test_git_authority_ignores_a_planted_replacement_object(committed, tmp_path):
    """Independent review on aaf1a502.

    `_git_tree_blobs` is the INDEPENDENT authority a release is checked
    against -- the one thing that still catches a same-name/wrong-tree release
    whose manifest was rewritten to match. A replacement ref would make that
    authority read the attacker's tree too, so manifest and Git check would
    agree on substituted content and `/opt/aicc/current` would select it with
    every gate reporting success.
    """
    module = _module()
    repo, tree, sha = committed
    manifest = tmp_path / "manifest.json"
    _record(module, tree, manifest, release_id=sha)

    attacker = tmp_path / "attacker"
    _git(tmp_path, "clone", "--quiet", str(repo), str(attacker))
    (attacker / "command_center" / "worker.py").write_text("import os  # implanted\n")
    _git(attacker, "add", "-A")
    _git(attacker, "commit", "-m", "implanted")
    evil = _git(attacker, "rev-parse", "HEAD")
    _git(repo, "fetch", "--quiet", str(attacker), f"{evil}:refs/heads/implanted")
    _git(repo, "replace", "-f", sha, evil)

    # The replacement is in force for an unhardened read of the same repo ...
    assert "implanted" in _git(repo, "show", f"{sha}:command_center/worker.py")

    # ... and the release still verifies against the genuinely committed tree.
    module.verify_release_manifest(
        tree, manifest, sha, repo_root=repo, trusted_uid=UID, trusted_gid=GID
    )


def test_the_two_git_hardening_lists_cannot_drift(tmp_path):
    """`ops/aicc_exact_sha_bootstrap.py` is extracted from Git as a single blob
    and executed before this module exists, so it cannot import the list from
    here and the duplication is deliberate. What must not happen is drift: a
    knob added to one boundary and forgotten in the other. This is the pin.
    """
    import importlib.util

    transaction = _module()
    path = Path(__file__).parents[2] / "ops" / "aicc_exact_sha_bootstrap.py"
    spec = importlib.util.spec_from_file_location("aicc_exact_sha_bootstrap", path)
    assert spec and spec.loader
    bootstrap = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bootstrap
    spec.loader.exec_module(bootstrap)

    assert transaction.GIT_CONFIG_FREE == bootstrap.GIT_CONFIG_FREE
    assert (
        transaction._git_safe_environment()["GIT_NO_REPLACE_OBJECTS"]
        == bootstrap._safe_environment(tmp_path)["GIT_NO_REPLACE_OBJECTS"]
        == "1"
    )


def test_installer_hardens_every_git_call_it_makes():
    """The shell installer runs `rev-parse` and `archive` as root against a
    checkout it does not own. Both must go through the hardened wrapper, and
    `archive` must name the verified release id rather than the symbolic HEAD.
    """
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-agent-principal-isolation.sh"
    ).read_text(encoding="utf-8")

    assert "git_trusted()" in installer
    assert "--no-replace-objects" in installer
    assert "GIT_NO_REPLACE_OBJECTS=1" in installer
    # No raw Git invocation outside the wrapper's own definition.
    raw = [
        line
        for line in installer.splitlines()
        if "/usr/bin/git" in line and "--no-replace-objects" not in line
    ]
    assert raw == [], raw
    assert 'archive --format=tar "$release_id"' in installer


def _transaction(module, tmp_path):
    root = tmp_path / "root"
    state = tmp_path / "state"
    (root / "opt/aicc").mkdir(parents=True)
    state.mkdir(mode=0o700)
    return module.FileTransaction(root, state), root, state


def test_recovery_refuses_to_select_an_unattested_release(tmp_path):
    """Independent review on cacfc257: the forward path proved a release
    before selection, but recovery reselected whatever `pending-release`
    named after checking only that the pathname looked right and the
    directory existed.

    That is the same missing-manifest admission the forward gate exists to
    close, reached through boot recovery instead -- and what it selects is the
    code every worker ExecStart runs as its own principal.
    """
    module = _module()
    transaction, root, _state = _transaction(module, tmp_path)
    release = root / "opt/aicc/releases" / RELEASE_ID
    release.mkdir(parents=True)
    (release / "marker").write_text("planted\n", encoding="utf-8")

    with pytest.raises(module.ReleaseRefused, match="missing or unsafe"):
        transaction.verify_release_selection(f"releases/{RELEASE_ID}")


def test_recovery_refuses_a_release_that_drifted_since_it_was_recorded(tmp_path):
    """A prior generation was proven once; that says nothing about now."""
    module = _module()
    transaction, root, state = _transaction(module, tmp_path)
    release = root / "opt/aicc/releases" / RELEASE_ID
    release.mkdir(parents=True)
    (release / "marker").write_text("release\n", encoding="utf-8")
    (state / "releases").mkdir(mode=0o700)
    module.record_release_manifest(
        release,
        transaction.release_manifest_path(RELEASE_ID),
        RELEASE_ID,
        trusted_uid=UID,
        trusted_gid=GID,
    )
    transaction.verify_release_selection(f"releases/{RELEASE_ID}")

    (release / "marker").write_text("implanted\n", encoding="utf-8")

    with pytest.raises(module.ReleaseRefused, match="does not match manifest"):
        transaction.verify_release_selection(f"releases/{RELEASE_ID}")


def test_rollback_and_uninstall_prove_the_release_they_restore():
    """The shell paths that repoint `/opt/aicc/current` on rollback and
    uninstall must go through the same proof, and rollback must remove the
    selector rather than restore an unprovable release: stopped units beat
    running unproven code as root.
    """
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-agent-principal-isolation.sh"
    ).read_text(encoding="utf-8")

    rollback = installer[installer.index("rollback() {") :]
    assert "release-verify" in rollback.split("trap rollback")[0]
    uninstall = installer[installer.index('if [ "${1:-}" = "--uninstall" ]') :]
    uninstall = uninstall.split("AICC_AGENT_PRINCIPAL_ISOLATION_UNINSTALLED")[0]
    # Compare COMMANDS, not prose: the comments explaining this ordering name
    # the very commands being ordered.
    commands = [
        line.strip()
        for line in uninstall.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    def first(fragment: str) -> int:
        for index, line in enumerate(commands):
            if fragment in line:
                return index
        raise AssertionError(f"uninstall never runs {fragment!r}")

    # The proof must precede every privileged mutation: this branch has no
    # rollback trap, so a check that runs after the service disables and the
    # file transaction can only report a partial uninstall it cannot undo
    # (independent review on 25eb0a0c).
    assert first("release-verify") < first("systemctl disable")
    assert first("release-verify") < first("run_transaction uninstall")
    assert "previous release failed verification; selector removed" in installer
