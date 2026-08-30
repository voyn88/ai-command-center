"""Adversarial coverage for the content-addressed toolchain installer.

This code downloads an archive and extracts it as root, then points
`/opt/aicc/toolchains/current` at the result -- and every agent lane executes
what that selector resolves to. The archive is the untrusted input; these tests
pin what must be refused before any of it reaches disk or the selector.

Everything runs as the invoking (unprivileged) user: the module takes the
trusted uid/gid as parameters exactly so its guarantees are testable without
root, and production passes 0/0.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_toolchain_install.py"
    spec = importlib.util.spec_from_file_location("aicc_toolchain_install", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


UID = os.geteuid()
GID = os.getegid()
DIGEST = "e" * 64


def _lock(tmp_path: Path, **overrides) -> Path:
    payload = {
        "platform": "linux-x64",
        "packages": {"@anthropic-ai/claude-code": "2.1.231"},
        "executables": {"claude": "pkg/claude"},
        "artifact_sha256": DIGEST,
        "release_tag": "toolchain-2026.08.30",
    }
    payload.update(overrides)
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _archive(entries: list[tarfile.TarInfo], bodies: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for entry in entries:
            body = bodies.get(entry.name)
            archive.addfile(entry, io.BytesIO(body) if body is not None else None)
    return buffer.getvalue()


def _file(name: str, mode: int = 0o755, size: int = 4) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.mode = mode
    info.size = size
    return info


def test_lock_without_a_pinned_digest_is_refused(tmp_path):
    """An unpinned lock is the npm-resolving behaviour in disguise: it would
    let whatever the tag currently points at become production's toolchain."""
    module = _module()
    with pytest.raises(module.ToolchainRefused, match="does not pin a sha256"):
        module.load_lock(_lock(tmp_path, artifact_sha256=None))


def test_lock_with_a_git_sha_where_a_content_address_belongs_is_refused(tmp_path):
    module = _module()
    with pytest.raises(module.ToolchainRefused, match="does not pin a sha256"):
        module.load_lock(_lock(tmp_path, artifact_sha256="a" * 40))


def test_lock_declaring_an_escaping_executable_path_is_refused(tmp_path):
    """The lock names what production will execute; a traversal there would
    point the selector outside the proven tree."""
    module = _module()
    with pytest.raises(module.ToolchainRefused, match="unsafe executable path"):
        module.load_lock(_lock(tmp_path, executables={"claude": "../../bin/sh"}))
    with pytest.raises(module.ToolchainRefused, match="unsafe executable path"):
        module.load_lock(_lock(tmp_path, executables={"claude": "/bin/sh"}))


def test_lock_declaring_an_unsafe_executable_name_is_refused(tmp_path):
    module = _module()
    with pytest.raises(module.ToolchainRefused, match="unsafe executable name"):
        module.load_lock(_lock(tmp_path, executables={"../claude": "pkg/claude"}))


def test_a_digest_mismatch_refuses_the_download(tmp_path, monkeypatch):
    """The whole content-addressing claim rests here."""
    module = _module()
    lock = json.loads(_lock(tmp_path).read_text(encoding="utf-8"))

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _limit):
            return b"not the artifact"

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: _Response())

    with pytest.raises(module.ToolchainRefused, match="digest mismatch"):
        module.fetch_artifact(lock, "linux-x64")


def test_a_matching_digest_is_accepted(tmp_path, monkeypatch):
    module = _module()
    payload = b"the real artifact"
    lock = json.loads(
        _lock(tmp_path, artifact_sha256=hashlib.sha256(payload).hexdigest()).read_text(
            encoding="utf-8"
        )
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _limit):
            return payload

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: _Response())

    assert module.fetch_artifact(lock, "linux-x64") == payload


def test_traversal_entry_is_refused_before_extraction(tmp_path):
    module = _module()
    archive = _archive([_file("../escape")], {"../escape": b"evil"})
    with pytest.raises(module.ToolchainRefused, match="unsafe path"):
        module.extract_artifact(archive, tmp_path / "staging")


def test_absolute_entry_is_refused(tmp_path):
    module = _module()
    archive = _archive([_file("/etc/cron.d/evil")], {"/etc/cron.d/evil": b"evil"})
    with pytest.raises(module.ToolchainRefused, match="unsafe path"):
        module.extract_artifact(archive, tmp_path / "staging")


def test_hardlink_entry_is_refused(tmp_path):
    """A hardlink is an alias into an inode the manifest already proved --
    frozen by mode, but still writable through the other name."""
    module = _module()
    info = tarfile.TarInfo("pkg/alias")
    info.type = tarfile.LNKTYPE
    info.linkname = "pkg/claude"
    archive = _archive([_file("pkg/claude"), info], {"pkg/claude": b"real"})
    with pytest.raises(module.ToolchainRefused, match="hardlink"):
        module.extract_artifact(archive, tmp_path / "staging")


def test_symlink_escaping_the_tree_is_refused(tmp_path):
    module = _module()
    info = tarfile.TarInfo("bin/claude")
    info.type = tarfile.SYMTYPE
    info.linkname = "../../../../bin/sh"
    archive = _archive([info], {})
    with pytest.raises(module.ToolchainRefused, match="symlink escapes"):
        module.extract_artifact(archive, tmp_path / "staging")


def test_absolute_symlink_is_refused(tmp_path):
    module = _module()
    info = tarfile.TarInfo("bin/claude")
    info.type = tarfile.SYMTYPE
    info.linkname = "/bin/sh"
    archive = _archive([info], {})
    with pytest.raises(module.ToolchainRefused, match="symlink escapes"):
        module.extract_artifact(archive, tmp_path / "staging")


def test_setuid_entry_is_refused(tmp_path):
    """Extracted as root, a setuid bit in the archive is a privilege grant."""
    module = _module()
    archive = _archive([_file("pkg/claude", mode=0o4755)], {"pkg/claude": b"real"})
    with pytest.raises(module.ToolchainRefused, match="setuid/setgid"):
        module.extract_artifact(archive, tmp_path / "staging")


def test_device_entry_is_refused(tmp_path):
    module = _module()
    info = tarfile.TarInfo("pkg/dev")
    info.type = tarfile.CHRTYPE
    info.mode = 0o600
    archive = _archive([info], {})
    with pytest.raises(module.ToolchainRefused, match="unsupported artifact entry"):
        module.extract_artifact(archive, tmp_path / "staging")


def test_an_empty_archive_is_refused(tmp_path):
    module = _module()
    with pytest.raises(module.ToolchainRefused, match="empty"):
        module.extract_artifact(_archive([], {}), tmp_path / "staging")


def test_a_contained_symlink_is_allowed(tmp_path):
    """The artifact's own `bin/` entries are relative links into `pkg/`; the
    guard must refuse escapes without refusing the normal shape."""
    module = _module()
    link = tarfile.TarInfo("bin/claude")
    link.type = tarfile.SYMTYPE
    link.linkname = "../pkg/claude"
    archive = _archive([_file("pkg/claude"), link], {"pkg/claude": b"real"})
    staging = tmp_path / "staging"

    module.extract_artifact(archive, staging, trusted_uid=UID, trusted_gid=GID)

    assert (staging / "bin/claude").is_symlink()
    assert os.readlink(staging / "bin/claude") == "../pkg/claude"


def test_declared_executable_missing_from_the_tree_is_refused(tmp_path):
    module = _module()
    release = tmp_path / "release"
    (release / "pkg").mkdir(parents=True)
    with pytest.raises(module.ToolchainRefused, match="is missing"):
        module._require_declared_executables(
            release, {"executables": {"claude": "pkg/claude"}}, trusted_uid=UID
        )


def test_declared_executable_that_is_not_executable_is_refused(tmp_path):
    module = _module()
    release = tmp_path / "release"
    (release / "pkg").mkdir(parents=True)
    target = release / "pkg" / "claude"
    target.write_bytes(b"real")
    target.chmod(0o644)
    with pytest.raises(module.ToolchainRefused, match="not executable"):
        module._require_declared_executables(
            release, {"executables": {"claude": "pkg/claude"}}, trusted_uid=UID
        )


def test_declared_executable_that_is_world_writable_is_refused(tmp_path):
    module = _module()
    release = tmp_path / "release"
    (release / "pkg").mkdir(parents=True)
    target = release / "pkg" / "claude"
    target.write_bytes(b"real")
    target.chmod(0o777)
    with pytest.raises(module.ToolchainRefused, match="group/world writable"):
        module._require_declared_executables(
            release, {"executables": {"claude": "pkg/claude"}}, trusted_uid=UID
        )


def test_the_selector_is_replaced_atomically(tmp_path):
    """`current` must never be observed absent: units resolve their ExecStart
    through it, and a gap is a failed start rather than an old generation."""
    module = _module()
    root = tmp_path / "toolchains"
    (root / "releases" / DIGEST).mkdir(parents=True)
    previous = "f" * 64
    (root / "releases" / previous).mkdir(parents=True)
    (root / "current").symlink_to(Path("releases") / previous)

    module.select_toolchain(DIGEST, root=root)

    assert os.readlink(root / "current") == f"releases/{DIGEST}"
    assert not list(root.glob(".current.*")), "no temporary selector may survive"


def test_nothing_in_the_installer_executes_a_subprocess():
    """The finding that blocked the principal-isolation P0 was production
    resolving npm as root -- resolving a package and running its lifecycle
    scripts with root privileges.

    Pinned against the module's AST rather than its text: the docstrings here
    necessarily discuss npm, and a substring scan would either fail on the
    prose or pass on a comment that hid a real call. What must be true is that
    this module spawns nothing at all -- no subprocess, no os.system, no exec
    family -- so verification never becomes execution."""
    import ast

    source = (
        Path(__file__).parents[2] / "ops" / "aicc_toolchain_install.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "subprocess" not in imported
    assert "pty" not in imported
    assert "multiprocessing" not in imported

    forbidden_calls = {"system", "popen", "execv", "execve", "execvp", "spawnv", "fork"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls, ast.dump(node.func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"eval", "exec", "compile"}, node.func.id


def _shell_commands(path: Path) -> list[tuple[int, str]]:
    """Executable lines of a shell script, with heredoc bodies removed.

    A first version of this scanned raw text and flagged the retirement notice
    these very scripts print -- prose that names npm precisely because it
    explains what no longer runs. What must be inspected is what the shell
    executes, so heredoc bodies and comments are excluded.
    """
    commands: list[tuple[int, str]] = []
    delimiter: str | None = None
    for number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        code = line.split("#", 1)[0].strip()
        if not code:
            continue
        if "<<" in code:
            marker = code.split("<<", 1)[1].strip()
            marker = marker.lstrip("-").strip().strip("'\"")
            if marker:
                delimiter = marker.split()[0]
        commands.append((number, code))
    return commands


def test_no_production_shell_path_resolves_npm():
    """The blocker this task closes, pinned across the privileged shell surface.

    `deploy/install-agent-toolchain.sh` used to run `npm install --global` as
    root. Checking that one file would not hold: what matters is that no shell
    script under deploy/ or ops/ invokes a package manager on the host. The
    Python side is pinned separately and more strictly -- see
    `test_nothing_in_the_installer_executes_a_subprocess`, which forbids
    spawning anything at all.
    """
    root = Path(__file__).parents[2]
    offenders = []
    for directory in ("deploy", "ops"):
        for path in sorted((root / directory).rglob("*.sh")):
            for number, code in _shell_commands(path):
                for token in ("npm install", "npm i ", "npm root", "npm exec", "npx "):
                    if token in code:
                        offenders.append(f"{path.relative_to(root)}:{number}: {code}")
    assert offenders == [], "production resolves packages:\n  " + "\n  ".join(offenders)


def test_the_retired_installer_fails_closed():
    """An operator following an old runbook must be told where the toolchain
    comes from now -- not left believing packages were installed."""
    path = Path(__file__).parents[2] / "deploy" / "install-agent-toolchain.sh"
    script = path.read_text(encoding="utf-8")
    assert "exit 1" in script
    assert "aicc_toolchain_install.py" in script
    executed = [code for _number, code in _shell_commands(path)]
    assert not any("npm" in code for code in executed), executed
