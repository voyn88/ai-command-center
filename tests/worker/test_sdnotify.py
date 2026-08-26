"""The sd_notify seam (SRV-06): one datagram, proven against a real socket.

The protocol is small enough to prove for real: a listening AF_UNIX datagram
socket stands in for systemd, so these tests assert the actual bytes on the
actual wire — not a mock of the sendmsg the implementation might not make.
"""

from __future__ import annotations

import os
import socket
import tempfile

from command_center.worker import sdnotify


def _listener():
    # Not pytest's tmp_path: its nested test-named directories overrun the
    # AF_UNIX sun_path limit (~104 bytes on macOS). mkdtemp stays short.
    path = os.path.join(tempfile.mkdtemp(prefix="sdn"), "n.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.settimeout(2)
    return sock, path


def test_without_notify_socket_every_call_is_a_silent_noop(monkeypatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.sd_notify("READY=1") is False


def test_state_reaches_the_socket_systemd_named(monkeypatch) -> None:
    sock, path = _listener()
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert sdnotify.sd_notify("WATCHDOG=1") is True
        assert sock.recv(64) == b"WATCHDOG=1"
    finally:
        sock.close()


def test_a_vanished_socket_is_swallowed_not_raised(monkeypatch) -> None:
    """A ping that cannot be sent is systemd's cue to restart us — the
    supervision the ping enables — so it must never also crash the work."""
    monkeypatch.setenv(
        "NOTIFY_SOCKET", os.path.join(tempfile.mkdtemp(prefix="sdn"), "gone.sock")
    )
    assert sdnotify.sd_notify("WATCHDOG=1") is False


def test_watchdog_interval_is_half_the_budget(monkeypatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "240000000")  # 240s, the unit's value
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert sdnotify.watchdog_interval_seconds() == 120.0


def test_watchdog_for_another_pid_is_not_our_obligation(monkeypatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "240000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
    assert sdnotify.watchdog_interval_seconds() is None


def test_absent_or_malformed_budget_means_no_watchdog(monkeypatch) -> None:
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert sdnotify.watchdog_interval_seconds() is None
    monkeypatch.setenv("WATCHDOG_USEC", "not-a-number")
    assert sdnotify.watchdog_interval_seconds() is None
