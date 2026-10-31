"""Root-owned self-deploy release helper.

Exercises the staging/select contract without requiring a real root sudo
grant: the helper is imported and invoked in-process under the test uid,
with a temporary release root that the test owns. The production path
additionally requires euid==0; those guards are unit-tested directly.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).parents[2] / "deploy" / "voyn-aicc-release-helper"


def _load():
    # The installed helper has no .py suffix (root:root 0755 under
    # /usr/local/sbin). importlib still needs a loader; feed it the bytes.
    source = HELPER.read_text(encoding="utf-8")
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("voyn_aicc_release_helper", loader=None)
    )
    exec(compile(source, str(HELPER), "exec"), module.__dict__)
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "tracked.txt").write_text("payload\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_helper_refuses_to_run_unless_root(monkeypatch):
    module = _load()
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as raised:
        module.main(["--release-root", "/tmp/x", "current"])
    assert raised.value.code == 1


def test_group_writable_release_root_is_repaired_to_0755(tmp_path, monkeypatch):
    """The helper's job is to keep /opt/aicc launcher-safe; a leftover 2775
    from the outage hand-fix must be repaired, not left for the next launch."""
    module = _load()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    root = tmp_path / "opt-aicc"
    root.mkdir(mode=0o775)
    # Non-root test uid cannot chown to 0:0. Stub ownership to look root-owned
    # after chmod so the post-repair invariant check can pass.
    real_stat = Path.stat
    real_lstat = Path.lstat

    def fake_stat(self, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        if self == root:
            return os.stat_result(
                (info.st_mode, info.st_ino, info.st_dev, info.st_nlink, 0, 0,
                 info.st_size, info.st_atime, info.st_mtime, info.st_ctime)
            )
        return info

    def fake_lstat(self, *args, **kwargs):
        info = real_lstat(self, *args, **kwargs)
        if self == root:
            return os.stat_result(
                (info.st_mode, info.st_ino, info.st_dev, info.st_nlink, 0, 0,
                 info.st_size, info.st_atime, info.st_mtime, info.st_ctime)
            )
        return info

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(module.os, "chown", lambda *a, **k: None)
    module._immutable_root_dir(root, create=False)
    assert stat.S_IMODE(real_stat(root).st_mode) == 0o755
    assert not (real_stat(root).st_mode & 0o022)


def test_stage_and_select_publish_a_root_hardened_release(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    # Ownership checks compare against uid 0; in the sandbox we are not root,
    # so replace the immutability prover with a mode-only check that still
    # refuses group-writable trees and creates the layout.
    def prove(path: Path, *, create: bool = False) -> None:
        if create:
            path.mkdir(parents=True, exist_ok=True)
            # Host umask 002 leaves group-write; the real helper (euid 0)
            # chmods back to 0755. Mirror that before the write-bit check.
            os.chmod(path, 0o755)
        info = path.stat()
        assert stat.S_ISDIR(info.st_mode)
        if stat.S_IMODE(info.st_mode) & 0o022:
            module._fail(f"group/other-writable: {path}")
        os.chmod(path, 0o755)

    monkeypatch.setattr(module, "_immutable_root_dir", prove)
    # Harden without chown (test uid cannot chown to 0:0).
    def harden(root: Path) -> None:
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            os.chmod(current, 0o555)
            for name in filenames:
                path = current / name
                if path.is_symlink():
                    continue
                info = path.lstat()
                executable = bool(info.st_mode & 0o111)
                os.chmod(path, 0o555 if executable else 0o444)

    monkeypatch.setattr(module, "_harden_tree", harden)

    repo, sha = _repo(tmp_path)
    release_root = tmp_path / "opt-aicc"
    venv = tmp_path / "runtime-venv"
    venv.mkdir()

    assert (
        module.main(
            [
                "--release-root",
                str(release_root),
                "stage",
                "--repo",
                str(repo),
                "--sha",
                sha,
                "--venv",
                str(venv),
            ]
        )
        == 0
    )
    release = release_root / "releases" / sha
    assert release.is_dir()
    assert (release / "tracked.txt").read_text(encoding="utf-8") == "payload\n"
    assert (release / ".aicc-release-sha").read_text(encoding="utf-8") == sha + "\n"
    assert (release / ".venv").is_symlink()
    assert not (stat.S_IMODE(release.stat().st_mode) & 0o222)

    assert (
        module.main(
            ["--release-root", str(release_root), "select", "--sha", sha]
        )
        == 0
    )
    assert (release_root / "current").readlink() == Path(f"releases/{sha}")
    assert (
        module.main(["--release-root", str(release_root), "current"]) == 0
    )


def test_sudoers_pins_release_root_to_opt_aicc():
    sudoers = (
        Path(__file__).parents[2] / "deploy/sudoers.d/voyn-aicc-self-deploy"
    ).read_text(encoding="utf-8")
    assert "--release-root /opt/aicc" in sudoers
    assert "voynadmin ALL=(root) NOPASSWD: VOYN_AICC_SELF_DEPLOY_RELEASE" in sudoers
    assert "/opt/aicc/releases" not in sudoers  # no direct write grant
