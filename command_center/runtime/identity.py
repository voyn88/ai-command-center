"""Process existence and identity, for launch-time recording and restart
reconciliation.

`kill(pid, 0)` alone only proves *some* process currently holds that pid — on
any long-running machine PIDs get reused, so a bare `kill(pid, 0) == 0` check
after a restart can easily be a completely unrelated process that happens to
have been assigned the same number. This module captures more than that: the
process's state, start time (`lstart`), and full command line, all from a single
`ps` call, so a later check can compare *identity*, not just "a pid exists".
POSIX zombies are deliberately treated as absent: they retain a PID until
their parent reaps them, but can no longer execute or own live work.

POSIX systems use `ps` (not `/proc`) because this project targets
macOS/BSD-likes as local development hosts; `/proc` does not exist on macOS.
Windows uses the process APIs directly. In particular, it must not emulate a
POSIX existence probe with ``os.kill(pid, 0)``: on Windows signal ``0`` is not
an existence-only operation and can interrupt the process being inspected.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


_POSIX_PS_START_SCHEME = "posix-ps-utc-v1:"
_LINUX_PROCFS_START_SCHEME = "linux-procfs-startticks-v1:"
_LINUX_PROCFS_BOOT_START_SCHEME = "linux-procfs-bootid-startticks-v2:"
_WINDOWS_FILETIME_START_SCHEME = "windows-filetime-v1:"
_KNOWN_START_SCHEMES = (
    _POSIX_PS_START_SCHEME,
    _LINUX_PROCFS_START_SCHEME,
    _LINUX_PROCFS_BOOT_START_SCHEME,
    _WINDOWS_FILETIME_START_SCHEME,
)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: str
    command: str

    def as_string(self) -> str:
        return f"{self.start_time}|{self.command}"


class ProcessQueryStatus(str, Enum):
    LIVE = "LIVE"
    ABSENT = "ABSENT"
    ZOMBIE = "ZOMBIE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProcessQuery:
    status: ProcessQueryStatus
    identity: ProcessIdentity | None = None


def _query_identity_windows(pid: int) -> ProcessQuery:
    """Query a Windows process without sending it a signal.

    A creation timestamp plus executable path is stable enough to detect PID
    reuse across supervisor restarts. Any inability to establish that complete
    identity is fail-closed as ``UNKNOWN``; only documented missing-process
    errors and an observed terminal exit code are ``ABSENT``.
    """
    if pid <= 0:
        return ProcessQuery(ProcessQueryStatus.ABSENT)
    if pid > 0xFFFFFFFF:
        return ProcessQuery(ProcessQueryStatus.UNKNOWN)

    try:
        return _query_identity_windows_native(pid)
    except Exception:
        # Loading/binding/calling the native API is an environmental boundary.
        # Fail closed: an incompatible host must never make live work look gone.
        return ProcessQuery(ProcessQueryStatus.UNKNOWN)


def _query_identity_windows_native(pid: int) -> ProcessQuery:
    """Implementation split out so native API failures have one boundary."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    still_active = 259
    wait_object_0 = 0
    wait_timeout = 0x102
    error_invalid_parameter = 87
    error_not_found = 1168

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.LPDWORD,
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize, False, pid
    )
    if not handle:
        error = ctypes.get_last_error()
        if error in {error_invalid_parameter, error_not_found}:
            return ProcessQuery(ProcessQueryStatus.ABSENT)
        return ProcessQuery(ProcessQueryStatus.UNKNOWN)

    try:
        wait_result = kernel32.WaitForSingleObject(handle, 0)
        if wait_result == wait_object_0:
            return ProcessQuery(ProcessQueryStatus.ABSENT)
        if wait_result != wait_timeout:
            return ProcessQuery(ProcessQueryStatus.UNKNOWN)

        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return ProcessQuery(ProcessQueryStatus.UNKNOWN)
        if exit_code.value != still_active:
            return ProcessQuery(ProcessQueryStatus.ABSENT)

        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return ProcessQuery(ProcessQueryStatus.UNKNOWN)

        image_buffer = ctypes.create_unicode_buffer(32768)
        image_size = wintypes.DWORD(len(image_buffer))
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, image_buffer, ctypes.byref(image_size)
        ):
            return ProcessQuery(ProcessQueryStatus.UNKNOWN)

        creation_ticks = (
            int(creation.dwHighDateTime) << 32
        ) | int(creation.dwLowDateTime)
        if creation_ticks == 0 or not image_buffer.value:
            return ProcessQuery(ProcessQueryStatus.UNKNOWN)

        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return ProcessQuery(ProcessQueryStatus.UNKNOWN)
        if exit_code.value != still_active:
            return ProcessQuery(ProcessQueryStatus.ABSENT)
        wait_result = kernel32.WaitForSingleObject(handle, 0)
        if wait_result == wait_object_0:
            return ProcessQuery(ProcessQueryStatus.ABSENT)
        if wait_result != wait_timeout:
            return ProcessQuery(ProcessQueryStatus.UNKNOWN)

        process_identity = ProcessIdentity(
            pid=pid,
            start_time=f"{_WINDOWS_FILETIME_START_SCHEME}{creation_ticks}",
            command=image_buffer.value,
        )
        return ProcessQuery(ProcessQueryStatus.LIVE, process_identity)
    finally:
        kernel32.CloseHandle(handle)


def _status_after_failed_ps(pid: int) -> ProcessQueryStatus:
    """Distinguish a confirmed absent pid from an unreadable live/unknown one."""
    if pid <= 0:
        return ProcessQueryStatus.ABSENT
    if os.name == "nt":
        return ProcessQueryStatus.UNKNOWN
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessQueryStatus.ABSENT
    except (PermissionError, OSError):
        return ProcessQueryStatus.UNKNOWN
    # The pid exists, but the identity query failed. It must not be treated as
    # gone: doing so could free a lease or terminalize work still in progress.
    return ProcessQueryStatus.UNKNOWN


def _query_identity_linux_procfs(pid: int) -> ProcessQuery | None:
    """Use Linux kernel start ticks, which do not have ``ps lstart``'s 1 s race.

    ``None`` means procfs is unavailable and the caller may use the weak,
    non-destructive ``ps`` fallback. Once procfs is present, read failures are
    fail-closed rather than silently weakening the identity scheme.
    """
    proc_root = f"/proc/{pid}"
    if not os.path.isdir("/proc"):
        return None
    stat_path = Path(proc_root) / "stat"
    try:
        first = stat_path.read_text(encoding="utf-8")
        second = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProcessQuery(ProcessQueryStatus.ABSENT)
    except (OSError, UnicodeError):
        return ProcessQuery(ProcessQueryStatus.UNKNOWN)
    def parse(raw: str) -> tuple[str, str, str] | None:
        closing = raw.rfind(")")
        opening = raw.find("(")
        if opening < 0 or closing <= opening:
            return None
        fields = raw[closing + 1 :].split()
        # After ``comm`` the first value is field 3 (state); starttime is
        # field 22. The other fields are mutable counters and must not be used
        # to decide whether two reads observed the same process.
        if len(fields) <= 19:
            return None
        start_ticks = fields[19]
        command = raw[opening + 1 : closing]
        if not start_ticks.isdigit() or not command:
            return None
        return fields[0], start_ticks, command

    first_snapshot = parse(first)
    second_snapshot = parse(second)
    if first_snapshot is None or second_snapshot is None:
        return ProcessQuery(ProcessQueryStatus.UNKNOWN)
    first_state, first_start_ticks, _first_command = first_snapshot
    second_state, start_ticks, command = second_snapshot
    # A different immutable birth tick means the PID was reused between reads.
    # Mutable procfs counters are expected to change and are deliberately
    # ignored. A zombie cannot become live again without PID reuse.
    if first_start_ticks != start_ticks or (
        first_state.startswith("Z") and not second_state.startswith("Z")
    ):
        return ProcessQuery(ProcessQueryStatus.UNKNOWN)
    if second_state.startswith("Z"):
        return ProcessQuery(ProcessQueryStatus.ZOMBIE)
    try:
        boot_id = str(
            uuid.UUID(
                (Path("/proc/sys/kernel/random/boot_id")).read_text(
                    encoding="ascii"
                ).strip()
            )
        )
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        # Start ticks restart from zero at boot. Without the kernel boot ID,
        # a durable identity could collide with an unrelated process after a
        # host reboot, so an available-but-incomplete procfs must fail closed.
        return ProcessQuery(ProcessQueryStatus.UNKNOWN)
    return ProcessQuery(
        ProcessQueryStatus.LIVE,
        ProcessIdentity(
            pid=pid,
            start_time=(
                f"{_LINUX_PROCFS_BOOT_START_SCHEME}{boot_id}:{start_ticks}"
            ),
            command=command,
        ),
    )


def query_identity(pid: int, *, timeout: float = 5.0) -> ProcessQuery:
    """Return a tri-state-safe process query.

    ``UNKNOWN`` is intentionally distinct from ``ABSENT``/``ZOMBIE`` so a
    failing or incompatible ``ps`` cannot make live work appear gone.
    """
    if os.name == "nt":
        return _query_identity_windows(pid)
    if sys.platform.startswith("linux"):
        procfs_query = _query_identity_linux_procfs(pid)
        if procfs_query is not None:
            return procfs_query

    ps_environment = dict(os.environ)
    # `ps lstart` is presentation text. Pin both locale and timezone so the
    # same kernel birth time cannot look different to two supervisor processes
    # and be mistaken for PID reuse.
    ps_environment.update({"LANG": "C", "LC_ALL": "C", "TZ": "UTC"})
    try:
        result = subprocess.run(
            ["ps", "-o", "state=,lstart=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=ps_environment,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ProcessQuery(_status_after_failed_ps(pid))
    if result.returncode != 0:
        return ProcessQuery(_status_after_failed_ps(pid))
    line = result.stdout.strip("\n")
    if not line.strip():
        return ProcessQuery(_status_after_failed_ps(pid))
    # `state` is the first token; its first character is the portable primary
    # process state on both Linux procps and BSD/macOS ps. A zombie has exited
    # and cannot execute, even though it keeps a PID until its parent reaps it.
    # `lstart` is a fixed-width field (`ddd mmm dd hh:mm:ss yyyy`-shaped, though
    # locale can change the exact text); `command` is everything after it. We
    # don't need to parse the timestamp, only compare it verbatim, so no
    # locale-specific date parsing is required here.
    parts = line.split(None, 6)
    if parts and parts[0].startswith("Z"):
        return ProcessQuery(ProcessQueryStatus.ZOMBIE)
    if len(parts) < 7:
        return ProcessQuery(ProcessQueryStatus.UNKNOWN)
    start_time = f"{_POSIX_PS_START_SCHEME}{' '.join(parts[1:6])}"
    command = parts[6]
    process_identity = ProcessIdentity(
        pid=pid, start_time=start_time, command=command
    )
    return ProcessQuery(ProcessQueryStatus.LIVE, process_identity)


def capture_identity(pid: int, *, timeout: float = 5.0) -> ProcessIdentity | None:
    """Return a confirmed live identity, else ``None``.

    Callers making a liveness decision must use :func:`query_identity` or
    :func:`process_exists`; ``None`` alone intentionally does not distinguish
    absent/zombie from an unqueryable process.
    """
    return query_identity(pid, timeout=timeout).identity


def process_exists(pid: int) -> bool:
    status = query_identity(pid).status
    return status not in {ProcessQueryStatus.ABSENT, ProcessQueryStatus.ZOMBIE}


def compare_recorded_identity(
    current: ProcessIdentity, recorded_identity: str
) -> bool | None:
    """Compare a live identity without making an unsafe upgrade inference.

    ``True`` means the immutable, versioned process-birth value matches;
    ``False`` means a known scheme proves PID reuse. ``None`` means the stored
    value predates scheme versioning (or is otherwise unknown), so a live PID
    must remain fail-closed until it is confirmed absent. Commands are not an
    ownership boundary because argv/process titles can legitimately change.
    """
    recorded_start, separator, _recorded_command = recorded_identity.partition("|")
    if not separator:
        return None
    if current.as_string() == recorded_identity:
        return True
    recorded_scheme = next(
        (prefix for prefix in _KNOWN_START_SCHEMES if recorded_start.startswith(prefix)),
        None,
    )
    current_scheme = next(
        (prefix for prefix in _KNOWN_START_SCHEMES if current.start_time.startswith(prefix)),
        None,
    )
    if recorded_scheme is None or current_scheme is None:
        return None
    if recorded_scheme != current_scheme:
        return None
    return recorded_start == current.start_time


def identity_matches(pid: int, recorded_identity: str | None) -> bool:
    """True only if a live PID has the same versioned birth identity.

    Command text may change without changing process ownership. False (never
    an exception) covers dead/reused PIDs and missing, legacy, or unknown
    recorded schemes that cannot be compared safely.
    """
    if not recorded_identity:
        return False
    query = query_identity(pid)
    if query.status is not ProcessQueryStatus.LIVE or query.identity is None:
        return False
    return compare_recorded_identity(query.identity, recorded_identity) is True
