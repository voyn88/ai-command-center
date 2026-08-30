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
import stat
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


def test_no_privileged_path_resolves_npm():
    """The blocker this task closes, pinned across the whole repository.

    An earlier version of this test scanned only `deploy/` and `ops/`, and an
    independent reviewer was right to call that out: the commit claimed "no
    production path resolves npm" while `scripts/start-web.sh` still ran
    `npm ci`. The claim is now stated as narrowly as it is true, and checked
    that widely -- every shell script in the repository is scanned, and the
    frontend build is allowed by name, with its reason, rather than by being
    outside the search.

    The distinction that matters is privilege, not location: the toolchain gate
    exists because the provider CLIs were installed AS ROOT on production
    hosts, resolving packages and running their lifecycle scripts. Building the
    web bundle is neither privileged nor part of the agent execution path.
    """
    root = Path(__file__).parents[2]
    # Allowed, with the reason each is not what the gate is about.
    allowed = {
        # Frontend bundle build. Runs as the invoking user, never root, and
        # produces web/dist -- nothing an agent executes.
        "scripts/start-web.sh",
    }
    offenders = []
    for path in sorted(root.rglob("*.sh")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith((".git/", "web/node_modules/", ".venv/")):
            continue
        if relative in allowed:
            continue
        for number, code in _shell_commands(path):
            for token in (
                "npm install",
                "npm ci",
                "npm i ",
                "npm root",
                "npm exec",
                "npx ",
            ):
                if token in code:
                    offenders.append(f"{relative}:{number}: {code}")
    assert offenders == [], "a shell path resolves packages:\n  " + "\n  ".join(
        offenders
    )


def test_no_operator_facing_message_recommends_a_global_npm_install():
    """A hint is a path too.

    `agent_runner` used to tell an operator whose CLI was missing to run
    `npm install -g @anthropic-ai/claude-code`. On a production host that is
    precisely what the installer refuses, and an operator following it with
    sudo would reintroduce the finding by hand. Independent review on 58b50b9.
    """
    root = Path(__file__).parents[2]
    offenders = []
    for path in sorted((root / "command_center").rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            lowered = line.lower()
            if "npm install -g" in lowered or "npm install --global" in lowered:
                if (
                    "forbid" in lowered
                    or "no longer" in lowered
                    or "used to" in lowered
                ):
                    continue
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    assert offenders == [], (
        "an operator-facing path recommends a global install:\n  "
        + "\n  ".join(offenders)
    )


def test_the_retired_installer_fails_closed():
    """An operator following an old runbook must be told where the toolchain
    comes from now -- not left believing packages were installed."""
    path = Path(__file__).parents[2] / "deploy" / "install-agent-toolchain.sh"
    script = path.read_text(encoding="utf-8")
    assert "exit 1" in script
    assert "aicc_toolchain_install.py" in script
    executed = [code for _number, code in _shell_commands(path)]
    assert not any("npm" in code for code in executed), executed


@pytest.mark.skipif(
    sys.platform != "linux",
    reason=(
        "publication needs renameat2(RENAME_NOREPLACE), which is Linux-only; "
        "the production hosts are Linux and CI runs this shard there"
    ),
)
def test_install_end_to_end_publishes_and_selects(tmp_path, monkeypatch):
    """The whole `install()` path, not its pieces.

    Every unit here passed while the live bootstrap still refused with
    "release manifest is missing or unsafe": `publish_release_tree` VERIFIES a
    manifest and does not create one, and nothing in `install()` recorded it.
    Testing the functions separately could never catch that -- only driving the
    path end to end does. This is the third time in this task that a test of a
    helper passed while its call site was wrong, so the pin is the path itself.
    """
    module = _module()
    root = tmp_path / "toolchains"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    payload_dir = tmp_path / "src"
    (payload_dir / "pkg").mkdir(parents=True)
    (payload_dir / "bin").mkdir()
    (payload_dir / "pkg" / "claude").write_bytes(b"#!/bin/sh\necho 2.1.231\n")
    (payload_dir / "pkg" / "claude").chmod(0o755)
    (payload_dir / "bin" / "claude").symlink_to("../pkg/claude")

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(payload_dir / "bin", arcname="bin")
        archive.add(payload_dir / "pkg", arcname="pkg")
    artifact = buffer.getvalue()
    digest = hashlib.sha256(artifact).hexdigest()

    lock_path = _lock(
        tmp_path,
        artifact_sha256=digest,
        executables={"claude": "pkg/claude"},
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _limit):
            return artifact

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: _Response())

    release = module.install(
        lock_path, root=root, state_dir=state, trusted_uid=UID, trusted_gid=GID
    )

    assert release == root / "releases" / digest
    assert (release / "pkg" / "claude").is_file()
    # The manifest must exist and authorise this release.
    manifest = state / "releases" / f"{digest}.json"
    assert manifest.is_file(), "publication left no manifest behind"
    module.verify_release_manifest(
        release,
        manifest,
        digest,
        trusted_uid=UID,
        trusted_gid=GID,
        id_pattern=module.ARTIFACT_ID_RE,
    )
    # And the selector points at it.
    assert os.readlink(root / "current") == f"releases/{digest}"
    assert (root / "current" / "bin" / "claude").resolve().is_file()

    # Running it again is idempotent: the release is proven, not rebuilt.
    again = module.install(
        lock_path, root=root, state_dir=state, trusted_uid=UID, trusted_gid=GID
    )
    assert again == release


@pytest.mark.skipif(sys.platform != "linux", reason="publication is Linux-only")
def test_install_repairs_every_ancestor_it_creates(tmp_path, monkeypatch):
    """Independent review on 78c019a.

    Creating `<root>/releases` with parents=True also creates `<root>` and its
    parent under the caller's umask. Repairing only the levels named in the
    loop leaves a group-writable ancestor, and the publication guard walks
    every ancestor -- so a clean first install fails one level higher instead
    of succeeding. This drives install() under a permissive umask and requires
    every directory it created to come out non-group-writable.
    """
    module = _module()
    # `/opt` itself is a system directory that already exists as 0755 in
    # production; the installer must not touch it. Model that, so the test
    # exercises the levels the installer actually creates.
    base = tmp_path / "opt"
    base.mkdir(mode=0o755)
    os.chmod(base, 0o755)
    root = base / "aicc" / "toolchains"
    state = tmp_path / "state"

    payload_dir = tmp_path / "src"
    (payload_dir / "pkg").mkdir(parents=True)
    (payload_dir / "pkg" / "claude").write_bytes(b"#!/bin/sh\nexit 0\n")
    (payload_dir / "pkg" / "claude").chmod(0o755)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(payload_dir / "pkg", arcname="pkg")
    artifact = buffer.getvalue()
    digest = hashlib.sha256(artifact).hexdigest()
    lock_path = _lock(
        tmp_path, artifact_sha256=digest, executables={"claude": "pkg/claude"}
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _limit):
            return artifact

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: _Response())

    previous = os.umask(0o002)
    try:
        module.install(
            lock_path, root=root, state_dir=state, trusted_uid=UID, trusted_gid=GID
        )
    finally:
        os.umask(previous)

    for directory in (root.parent, root, root / "releases"):
        mode = stat.S_IMODE(directory.stat().st_mode)
        assert not mode & 0o022, f"{directory} is group/world writable: {mode:o}"
