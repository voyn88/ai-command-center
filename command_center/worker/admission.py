"""Operator-controlled mutation admission, shared by existing worker lanes."""
from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path


class AdmissionRefused(Exception):
    def __init__(self, reason: str, *, contention: bool = False):
        super().__init__(reason)
        self.contention = contention


@contextmanager
def hold(project_id: str):
    if os.environ.get("AICC_MUTATING_EXECUTION_PAUSED") == "1":
        raise AdmissionRefused("mutating execution paused by operator")
    allowed = os.environ.get("AICC_MUTATING_PROJECT_ALLOWLIST")
    if allowed is not None and project_id not in {x.strip() for x in allowed.split(",") if x.strip()}:
        raise AdmissionRefused("project excluded from mutating execution")
    if os.environ.get("AICC_SINGLE_MUTATING_WRITER") != "1":
        yield
        return
    path = Path(os.environ.get("AICC_DATA_DIR", "/var/lib/aicc/data")) / "single-mutating-writer.lock"
    fd = None
    try:
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise AdmissionRefused("mutation admission lock is not worker-owned")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AdmissionRefused("another lane holds mutation admission", contention=True) from exc
        except OSError as exc:
            raise AdmissionRefused("mutation admission lock unavailable") from exc
        yield
    finally:
        if fd is not None:
            os.close(fd)
