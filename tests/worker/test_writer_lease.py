"""writer_lease.hold (VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE): the
full-lifecycle writer lease held via background renewal, independent of
`publish.publish_run`'s own around-the-push acquire.

Mirrors `tests/orchestrator/test_publish.py`'s style: a real shell script on
PATH stands in for the external `voyn-lease` tool, so the subprocess
boundary (argv shape, exit-code handling) is actually exercised rather than
stubbed out."""

from __future__ import annotations

import threading

import pytest

from command_center.worker.writer_lease import (
    WriterLeaseConfig,
    WriterLeaseUnavailable,
    hold,
)


def _cfg(tool: str, ttl: int = 600) -> WriterLeaseConfig:
    return WriterLeaseConfig(
        lease_tool=tool,
        repository="ai-command-center",
        owner="server-worker",
        session="s1",
        task="VOYN-W0-TEST",
        ttl=ttl,
    )


def _install_lease_tool(tmp_path, body: str):
    binary = tmp_path / "fake-voyn-lease"
    binary.write_text(f"#!/bin/sh\n{body}\n")
    binary.chmod(0o755)
    return binary


def test_hold_acquires_and_installs_hooks_then_releases_on_exit(tmp_path):
    calls = tmp_path / "calls.log"
    tool = _install_lease_tool(
        tmp_path, f'echo "$*" >> {calls}\nexit 0\n'
    )
    lease_lost = threading.Event()

    with hold(tmp_path, _cfg(str(tool)), lease_lost):
        pass

    log = calls.read_text()
    assert " acquire " in log
    assert " install-hooks " in log
    assert " release " in log
    # acquire/install-hooks happen before release, matching publish_run's own
    # acquire -> install-hooks -> ... -> release ordering.
    assert log.index(" acquire ") < log.index(" release ")
    assert log.index(" install-hooks ") < log.index(" release ")
    assert not lease_lost.is_set()


def test_hold_raises_when_initial_acquire_fails(tmp_path):
    tool = _install_lease_tool(
        tmp_path, "echo 'lease held by another writer' >&2\nexit 1\n"
    )
    lease_lost = threading.Event()

    with pytest.raises(WriterLeaseUnavailable, match="acquire_failed"):
        hold(tmp_path, _cfg(str(tool)), lease_lost)

    # A failed initial acquire must not itself trip forced cancellation --
    # nothing was ever held, so there is nothing to cancel.
    assert not lease_lost.is_set()


def test_hold_raises_when_install_hooks_fails_after_a_successful_acquire(tmp_path):
    calls = tmp_path / "calls.log"
    tool = _install_lease_tool(
        tmp_path,
        f'echo "$*" >> {calls}\n'
        'case "$3" in\n'
        "  install-hooks) exit 1 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    lease_lost = threading.Event()

    with pytest.raises(WriterLeaseUnavailable, match="install_hooks_failed"):
        hold(tmp_path, _cfg(str(tool)), lease_lost)

    log = calls.read_text()
    assert " acquire " in log
    assert " install-hooks " in log
    assert " release " not in log  # nothing to release: acquire's own row
    # is left for the tool's own TTL/verify semantics, matching publish.py's
    # documented handling of a failed install-hooks after a real acquire.


def test_a_renewal_failure_sets_lease_lost_and_forces_cancellation(tmp_path):
    """The core acceptance behaviour: a lease lost mid-run (another writer
    took it over, the authority rejected a renewal) must set the SAME
    `lease_lost` event the caller wires into `run_claude_code` as
    `cancel_event` (#349) -- no second, parallel cancellation mechanism."""
    calls = tmp_path / "calls.log"
    # First acquire+install-hooks succeed (the initial `hold()` call); every
    # call after that fails, simulating the lease being lost the moment the
    # background thread tries its first renewal.
    tool = _install_lease_tool(
        tmp_path,
        f'echo "$*" >> {calls}\n'
        f"n=$(grep -c . {calls} 2>/dev/null || echo 0)\n"
        'if [ "$n" -le 2 ]; then exit 0; else exit 1; fi\n',
    )
    lease_lost = threading.Event()

    with hold(tmp_path, _cfg(str(tool), ttl=3), lease_lost):
        # ttl=3 -> renew interval is the module's floor (1s). Give the
        # background thread a few cycles to observe the failure.
        assert lease_lost.wait(timeout=10), "renewal failure never set lease_lost"

    log = calls.read_text()
    assert log.count(" acquire ") >= 2, "no renewal attempt was made"


def test_release_runs_even_when_the_caller_raises(tmp_path):
    calls = tmp_path / "calls.log"
    tool = _install_lease_tool(tmp_path, f'echo "$*" >> {calls}\nexit 0\n')
    lease_lost = threading.Event()

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with hold(tmp_path, _cfg(str(tool)), lease_lost):
            raise _Boom()

    assert " release " in calls.read_text()


def test_argv_shape_matches_publish_pys_lease_calls(tmp_path):
    """Both callers must construct identical identity argv (see
    `lease_client.lease_argv`, shared by `publish.py` and this module) so a
    renewal from this module and `publish_run`'s own re-acquire under the
    same identity land on the same lease row."""
    calls = tmp_path / "calls.log"
    tool = _install_lease_tool(tmp_path, f'echo "$*" >> {calls}\nexit 0\n')
    lease_lost = threading.Event()

    with hold(tmp_path, _cfg(str(tool)), lease_lost):
        pass

    log = calls.read_text()
    assert "--repository ai-command-center" in log
    assert "--owner server-worker" in log
    assert "--session s1" in log
    assert "--task VOYN-W0-TEST" in log
    assert "--pid " in log
    assert "--process-start " in log


def test_acquire_carries_the_configured_ttl_not_the_clis_own_default(tmp_path):
    """VOYN-W0-AICC-LEASE-TTL-CONTRACT-BROKEN (found live 2026-08-23):
    `--ttl` was never forwarded at all, so every acquire silently got the
    external CLI's own default (180s) instead of the 600s every config
    declared -- and the renewal thread, scheduling its first attempt at
    `ttl/3` against the CONFIGURED value, renewed 20s after the row had
    actually already expired under the real default. Every `acquire` call
    (initial and every renewal) must carry `--ttl <configured>`."""
    calls = tmp_path / "calls.log"
    tool = _install_lease_tool(tmp_path, f'echo "$*" >> {calls}\nexit 0\n')
    lease_lost = threading.Event()

    with hold(tmp_path, _cfg(str(tool), ttl=900), lease_lost):
        pass

    log = calls.read_text()
    # The fake tool logs `$*`, which (unlike argv) does not include the
    # tool's own path -- logged shape is `--repo <path> <verb> ...`, verb
    # is the 3rd token.
    acquire_lines = [ln for ln in log.splitlines() if ln.split()[2:3] == ["acquire"]]
    assert acquire_lines, "no acquire call recorded"
    for line in acquire_lines:
        assert "--ttl 900" in line, f"acquire call missing configured --ttl: {line!r}"


def test_renewal_is_scheduled_with_margin_before_the_granted_ttl_expires():
    """The invariant the live incident violated: renew_after must be
    comfortably less than the granted TTL, never merely close to it (a
    renewal scheduled at exactly the TTL, or later, races network/
    scheduling jitter and can lose every single time, not just under
    contention). `_RENEW_FRACTION=3` gives a 2x safety margin -- renewal
    at 1/3 of the TTL leaves 2/3 of it as slack before expiry."""
    from command_center.worker.writer_lease import _MIN_RENEW_INTERVAL_SECONDS, _RENEW_FRACTION

    for ttl in (60, 180, 600, 3600):
        renew_after = max(ttl / _RENEW_FRACTION, _MIN_RENEW_INTERVAL_SECONDS)
        assert renew_after < ttl, (
            f"renew_after={renew_after} must be strictly less than ttl={ttl}"
        )
        # At least 2x margin (renewal happens with over half the TTL still
        # to spare), not just "sometime before expiry".
        assert renew_after <= ttl / 2, (
            f"renew_after={renew_after} for ttl={ttl} leaves too little margin"
        )
