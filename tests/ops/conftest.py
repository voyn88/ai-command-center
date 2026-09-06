"""Shared shaping for the ops installer tests.

`host_shaped_fixture_roots` is opt-in per module (`pytestmark = ...
usefixtures`), not autouse: it changes how a test's own scaffolding is
created, and only the modules that drive the privileged installer need it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_PATHLIB_DEFAULT_DIRECTORY_MODE = 0o777
_HOST_DIRECTORY_MODE = 0o755
_HOST_FILE_MODE = 0o644


@pytest.fixture
def host_shaped_fixture_roots(monkeypatch):
    """Build fixture paths the way a host has them, under a permissive umask.

    These tests drive privileged installation code against a fixture root
    that stands in for `/`. The installer walks every component of a
    destination path from the filesystem root and refuses any component a
    non-owner could rename through, and refuses a source or registry file
    that is group- or world-writable -- which on a real host is never the
    case, because `/`, `/etc`, `/var/lib` and everything the repository
    installs from are root-owned and not group-writable.

    A fixture path created with `Path.mkdir()` or `Path.write_bytes()` and no
    explicit mode gets 0o777/0o666 masked by whatever umask the person
    running pytest happens to have. Under the very common 0o002 that is
    0o775/0o664 -- group-writable, which the installer correctly refuses.
    That is a property of the test's own scaffolding, not of anything under
    test, so mode-less creation here gets the shape a host actually has.

    Calls that pass an explicit mode are left exactly alone, and the
    installer always passes one: it creates every directory it owns with a
    mode and then chmods it, so its own umask independence stays fully under
    test and is asserted directly in
    `test_generation_directories_are_private_under_a_permissive_umask`.

    The umask is pinned to the permissive 0o002 for the same reason: the
    installer must not depend on inheriting a restrictive one, so these
    tests prove it under the loosest umask an operator plausibly has rather
    than under whichever one the invoking shell supplies.
    """
    previous = os.umask(0o002)
    real_mkdir = Path.mkdir
    real_write_bytes = Path.write_bytes
    real_write_text = Path.write_text

    def mkdir(self, mode=_PATHLIB_DEFAULT_DIRECTORY_MODE, parents=False, exist_ok=False):
        created = []
        if mode == _PATHLIB_DEFAULT_DIRECTORY_MODE:
            probe = self
            while not probe.exists():
                created.append(probe)
                if probe.parent == probe:
                    break
                probe = probe.parent
        real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
        for path in created:
            if path.is_dir():
                os.chmod(path, _HOST_DIRECTORY_MODE)

    def write_bytes(self, data):
        fresh = not self.exists()
        result = real_write_bytes(self, data)
        if fresh:
            os.chmod(self, _HOST_FILE_MODE)
        return result

    def write_text(self, data, *args, **kwargs):
        fresh = not self.exists()
        result = real_write_text(self, data, *args, **kwargs)
        if fresh:
            os.chmod(self, _HOST_FILE_MODE)
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    monkeypatch.setattr(Path, "write_text", write_text)
    try:
        yield
    finally:
        os.umask(previous)
