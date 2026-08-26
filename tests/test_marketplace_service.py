"""Service-tier tests for the Wave-3 Marketplace
(``command_center.marketplace.service`` → ``runtime.db.marketplace``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases, so the runtime db the service writes is
throwaway. The service migrates it lazily on first use.

The install path is exercised through an **injected recording installer** — no
real code execution, no fetch — while the lifecycle transition and the audit log
around it are the real thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from command_center.api import marketplace_schemas as s
from command_center.api import models
from command_center.marketplace import service
from command_center.marketplace.installer import InstallOutcome


@dataclass
class RecordingInstaller:
    """A test double implementing the ``Installer`` protocol: it records the
    items it was asked to install and executes nothing."""

    name: str = "recording-installer"
    calls: list[str] = field(default_factory=list)
    outcome: InstallOutcome = field(
        default_factory=lambda: InstallOutcome(detail="recorded", metadata={"k": "v"})
    )

    def install(self, item: models.MarketItem) -> InstallOutcome:
        self.calls.append(item.id)
        return self.outcome


def _register(**overrides) -> models.MarketItem:
    payload = s.MarketItemCreate(name="Thing", kind="module")
    for key, value in overrides.items():
        setattr(payload, key, value)
    return service.register_item(payload)


# --- register / list / get ------------------------------------------------


def test_register_creates_listed_item() -> None:
    item = _register(name="Widget", kind="domain_pack", version="1.0.0",
                     provenance="channel:stable")
    assert item.id and item.status == "listed"
    assert item.kind == "domain_pack"
    assert item.provenance == "channel:stable"
    assert service.get_item(item.id).name == "Widget"


def test_register_rejects_bad_kind() -> None:
    with pytest.raises(ValueError):
        service.register_item(s.MarketItemCreate(name="x", kind="nonsense"))


def test_get_missing_item_returns_none() -> None:
    assert service.get_item("nope") is None


def test_list_filters_and_pages() -> None:
    _register(name="a", kind="module")
    _register(name="b", kind="plugin")
    page = service.list_items()
    assert page.limit == 100 and page.offset == 0
    assert len(page.items) == 2
    assert len(service.list_items(kind="plugin").items) == 1


# --- install lifecycle + log (the acceptance path) ------------------------


def test_install_transitions_and_logs_who_when_what() -> None:
    item = _register(name="Thing", kind="plugin", version="2.3.4",
                     provenance="url:https://example.test/x")
    installer = RecordingInstaller()

    installed = service.install_item(item.id, actor="alice", installer=installer)

    assert installed.status == "installed"
    assert installer.calls == [item.id]  # the seam was actually used

    log = service.get_install_log(item.id)
    assert len(log.entries) == 1
    entry = log.entries[0]
    # who / when / what version — the acceptance invariant of the log.
    assert entry.actor == "alice"
    assert entry.version == "2.3.4"
    assert entry.installed_at
    # provenance carried verbatim from the listing onto the trail line.
    assert entry.provenance == "url:https://example.test/x"
    assert entry.installer == "recording-installer"
    assert entry.detail == "recorded"
    assert entry.metadata == {"k": "v"}


def test_install_defaults_to_safe_null_installer() -> None:
    """With no installer injected, the default performs no code execution but
    the lifecycle + log are still real."""
    item = _register(name="Thing", kind="module", version="0.1.0")
    installed = service.install_item(item.id, actor="bob")
    assert installed.status == "installed"
    entry = service.get_install_log(item.id).entries[0]
    assert entry.installer == "null-installer"
    assert entry.actor == "bob" and entry.version == "0.1.0"


def test_install_is_idempotent_no_duplicate_log() -> None:
    item = _register(name="Thing", kind="module")
    installer = RecordingInstaller()

    first = service.install_item(item.id, actor="alice", installer=installer)
    second = service.install_item(item.id, actor="alice", installer=installer)

    assert first.status == second.status == "installed"
    # Repeat install is a no-op: installer not called again, no second log line.
    assert installer.calls == [item.id]
    assert len(service.get_install_log(item.id).entries) == 1


def test_install_missing_item_raises_not_found() -> None:
    with pytest.raises(service.MarketItemNotFoundError):
        service.install_item("nope", actor="alice")


def test_failed_installer_leaves_item_listed_and_unlogged() -> None:
    item = _register(name="Thing", kind="module")

    @dataclass
    class BoomInstaller:
        name: str = "boom"

        def install(self, item: models.MarketItem) -> InstallOutcome:
            raise RuntimeError("materialisation failed")

    with pytest.raises(RuntimeError):
        service.install_item(item.id, actor="alice", installer=BoomInstaller())

    # Nothing was committed: still listed, no trail line.
    assert service.get_item(item.id).status == "listed"
    assert service.get_install_log(item.id).entries == []


def test_get_install_log_missing_item_returns_none() -> None:
    assert service.get_install_log("nope") is None
