"""``CONSOLE_OPERATIONS`` is a second closed inventory, deliberately not
folded into ``OPERATIONS`` — see the module docstring on
``command_center.http_auth.authz``. This file is the proof the docstring
promises: the two inventories never overlap, an unknown console operation is
a loud error rather than a silent denial, and ``is_console_permitted`` is
deny-by-default in exactly the same shape as ``is_permitted``.
"""

from __future__ import annotations

import json

import pytest

from command_center.http_auth import authz


def test_console_operations_and_http_operations_are_disjoint():
    """A name valid on one surface must never accidentally authorize the
    other, even though both are checked against the same grant file."""
    assert authz.CONSOLE_OPERATIONS.isdisjoint(authz.OPERATIONS)


def test_an_unknown_console_operation_is_an_error_not_a_denial():
    with pytest.raises(authz.UnknownOperationError):
        authz.is_console_permitted("operator:one", "console:not-a-real-operation")


def test_an_http_operation_is_not_a_valid_console_operation():
    """The two inventories are checked separately: an operation that is real
    for the HTTP surface is still unknown to the console gate."""
    with pytest.raises(authz.UnknownOperationError):
        authz.is_console_permitted("operator:one", "dispatch:assign")


def test_a_console_operation_is_not_a_valid_http_operation():
    with pytest.raises(authz.UnknownOperationError):
        authz.is_permitted("operator:one", "console:start_task")


def test_an_empty_grant_map_denies_every_console_operation(grants):
    grants({})
    assert authz.is_console_permitted("operator:one", "console:start_task") is False


def test_no_grant_configuration_at_all_denies_every_console_operation(monkeypatch):
    monkeypatch.delenv(authz.GRANTS_FILE_ENV, raising=False)
    authz.reset_grants_cache()
    assert authz.is_console_permitted("operator:one", "console:start_task") is False


def test_a_console_grant_is_per_operation_not_per_principal(grants):
    """Being granted the console's start-task operation is not being granted
    an unrelated one — the same per-operation shape ``is_permitted`` has."""
    grants({"operator:one": ["console:start_task"]})
    assert authz.is_console_permitted("operator:one", "console:start_task") is True
    assert authz.is_console_permitted("operator:two", "console:start_task") is False


def test_a_console_grant_does_not_authorize_the_http_surface(grants):
    """Same grant file, two inventories: granting the console operation must
    not also make ``is_permitted`` (checked against ``OPERATIONS``) say yes
    for anything, since the console operation isn't even in that inventory."""
    grants({"operator:one": ["console:start_task"]})
    with pytest.raises(authz.UnknownOperationError):
        authz.is_permitted("operator:one", "console:start_task")


def test_a_grant_file_naming_an_unknown_console_operation_is_refused(monkeypatch, tmp_path):
    path = tmp_path / "grants.json"
    path.write_text(
        json.dumps({"operator:one": ["console:retired"]}), encoding="utf-8"
    )
    monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(path))
    authz.reset_grants_cache()

    with pytest.raises(authz.GrantsConfigurationError) as caught:
        authz.load_grants()
    assert "console:retired" in str(caught.value)
    authz.reset_grants_cache()


def test_a_grant_file_may_mix_http_and_console_operations_for_one_principal(monkeypatch, tmp_path):
    """One grant file governs both surfaces — a principal may legitimately
    hold grants on each without either inventory rejecting the other's name."""
    path = tmp_path / "grants.json"
    path.write_text(
        json.dumps({"operator:one": ["dispatch:assign", "console:start_task"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(path))
    authz.reset_grants_cache()

    assert authz.is_permitted("operator:one", "dispatch:assign") is True
    assert authz.is_console_permitted("operator:one", "console:start_task") is True
    authz.reset_grants_cache()
