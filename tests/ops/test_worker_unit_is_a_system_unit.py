"""A systemd *user* unit is not this project's security boundary.

Both worker unit families (`aicc-worker.service`, the single-lane SRV-05
unit, and `voyn-aicc-worker@.service`, the preprod lane template) declare a
sandbox (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
`PrivateDevices`, ...) that only works because the system manager performs a
real privilege transition before applying it. A user unit
(`~/.config/systemd/user`, run via `systemctl --user`) executes under the
invoking account's own systemd instance at that same UID: the operator who
could edit or bypass such a unit already owns everything the directives
would otherwise withhold, so the "sandbox" would be decorative.

`WantedBy=multi-user.target` is the mechanical, un-fakeable signal that a
unit targets the system manager -- `multi-user.target` does not exist in a
user manager, whose own boot target is `default.target`. These tests pin
that line, and the header warning explaining why it matters, so neither can
be silently dropped by a future edit (VOYN-W0-AICC-SRV-05-USER-UNIT-IS-NOT-
A-BOUNDARY).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]

WORKER_UNITS = (
    "deploy/systemd/aicc-worker.service",
    "deploy/systemd/voyn-aicc-worker@.service",
)


@pytest.mark.parametrize("relative_path", WORKER_UNITS)
def test_worker_unit_targets_the_system_manager(relative_path: str) -> None:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    assert "[Install]" in text
    install_section = text.split("[Install]", 1)[1]
    assert "WantedBy=multi-user.target" in install_section
    # default.target is the user manager's own boot target; its presence
    # here would mean the unit is meant to double as a user unit too.
    assert "default.target" not in install_section


@pytest.mark.parametrize("relative_path", WORKER_UNITS)
def test_worker_unit_declares_a_privilege_transition(relative_path: str) -> None:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    # `User=` is only a real UID drop when the unit is loaded by the
    # root-owned system manager; asserting it alongside `NoNewPrivileges`
    # ties the two together mechanically. The lane template
    # (voyn-aicc-worker@.service) deliberately carries only
    # NoNewPrivileges itself -- the rest of its mount/network sandbox
    # arrives solely via the principal-isolation drop-in, checked below, so
    # the fail-closed flag can never ship pre-armed in the base unit
    # (independent-review REJECT on b6ea174).
    assert "User=aicc-worker" in text
    assert "NoNewPrivileges=" in text


def test_base_worker_unit_carries_its_own_mount_network_sandbox() -> None:
    text = (ROOT / "deploy/systemd/aicc-worker.service").read_text(encoding="utf-8")
    for directive in ("NoNewPrivileges=", "ProtectSystem=strict", "ProtectHome="):
        assert directive in text, f"aicc-worker.service dropped {directive!r}"


def test_lane_worker_sandbox_arrives_only_via_the_isolation_drop_in() -> None:
    template = (ROOT / "deploy/systemd/voyn-aicc-worker@.service").read_text(
        encoding="utf-8"
    )
    dropin = (
        ROOT / "deploy/systemd/voyn-aicc-worker-principal-isolation.conf"
    ).read_text(encoding="utf-8")
    for directive in ("ProtectSystem=strict", "ProtectHome="):
        assert directive not in template, (
            f"voyn-aicc-worker@.service must not pre-arm {directive!r} outside "
            "the drop-in"
        )
        assert directive in dropin, f"drop-in dropped {directive!r}"


def test_base_worker_unit_documents_the_user_unit_hazard() -> None:
    text = (ROOT / "deploy/systemd/aicc-worker.service").read_text(encoding="utf-8")
    assert "/etc/systemd/system" in text
    assert "never `systemctl --user`" in text
    assert "user unit is not this boundary" in text


def test_lane_worker_unit_documents_the_user_unit_hazard() -> None:
    text = (ROOT / "deploy/systemd/voyn-aicc-worker@.service").read_text(
        encoding="utf-8"
    )
    assert "/etc/systemd/system" in text
    assert "user unit" in text
