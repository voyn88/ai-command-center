"""TLS enforcement and token provisioning."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading

import pytest

from native_gateway.provision import main, mint, revoke
from native_gateway.serve import TLSConfigurationError, resolve_tls


def test_serve_refuses_to_start_without_tls():
    with pytest.raises(TLSConfigurationError):
        resolve_tls(env={})


def test_serve_refuses_missing_tls_files(tmp_path):
    with pytest.raises(TLSConfigurationError):
        resolve_tls(
            env={
                "AICC_GATEWAY_TLS_CERT": str(tmp_path / "missing-cert.pem"),
                "AICC_GATEWAY_TLS_KEY": str(tmp_path / "missing-key.pem"),
            }
        )


def test_serve_accepts_existing_tls_files(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("cert")
    key.write_text("key")
    resolved = resolve_tls(
        env={"AICC_GATEWAY_TLS_CERT": str(cert), "AICC_GATEWAY_TLS_KEY": str(key)}
    )
    assert resolved == (cert, key)


def test_mint_stores_only_hash_with_tight_permissions(tmp_path):
    registry_path = tmp_path / "tokens.json"
    token = mint(registry_path, "iphone-owner-01", "Owner iPhone")
    stored = registry_path.read_text(encoding="utf-8")
    assert token not in stored  # plaintext never persisted
    entry = json.loads(stored)["devices"][0]
    assert entry["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert entry["scope"] == "read"
    if os.name == "posix":  # permission bits are only meaningful on POSIX
        mode = stat.S_IMODE(registry_path.stat().st_mode)
        assert mode == 0o600


def test_mint_rejects_duplicate_device(tmp_path):
    registry_path = tmp_path / "tokens.json"
    mint(registry_path, "dev-1", "")
    with pytest.raises(SystemExit):
        mint(registry_path, "dev-1", "")


def test_revoke_disables_the_device_and_leaves_an_audit_trail(tmp_path):
    registry_path = tmp_path / "tokens.json"
    mint(registry_path, "dev-1", "Owner iPhone")

    assert revoke(registry_path, "dev-1", "lost device") is True

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    device = registry["devices"][0]
    assert device["disabled"] is True
    assert device["disabled_reason"] == "lost device"
    assert device["disabled_at"]
    audit = registry["audit"]
    assert len(audit) == 1
    assert audit[0]["action"] == "revoke"
    assert audit[0]["device_id"] == "dev-1"
    assert audit[0]["reason"] == "lost device"
    if os.name == "posix":
        mode = stat.S_IMODE(registry_path.stat().st_mode)
        assert mode == 0o600


def test_revoking_an_already_disabled_device_is_not_an_error(tmp_path):
    registry_path = tmp_path / "tokens.json"
    mint(registry_path, "dev-1", "")
    assert revoke(registry_path, "dev-1", "first reason") is True

    assert revoke(registry_path, "dev-1", "second reason") is False

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    device = registry["devices"][0]
    # The first revoke's reason stands; a no-op re-revoke does not overwrite it.
    assert device["disabled_reason"] == "first reason"
    assert len(registry["audit"]) == 1


def test_revoking_an_unknown_device_is_an_error(tmp_path):
    registry_path = tmp_path / "tokens.json"
    mint(registry_path, "dev-1", "")
    with pytest.raises(SystemExit):
        revoke(registry_path, "dev-does-not-exist", "reason")


def test_revoking_against_a_missing_registry_is_an_error(tmp_path):
    with pytest.raises(SystemExit):
        revoke(tmp_path / "does-not-exist.json", "dev-1", "reason")


def test_cli_mint_then_revoke(tmp_path, capsys):
    registry_path = tmp_path / "tokens.json"

    assert main(["mint", "--registry", str(registry_path), "--device-id", "dev-1"]) == 0
    minted = capsys.readouterr().out
    assert "Device token" in minted

    assert (
        main(
            [
                "revoke",
                "--registry",
                str(registry_path),
                "--device-id",
                "dev-1",
                "--reason",
                "lost device",
            ]
        )
        == 0
    )
    assert "revoked" in capsys.readouterr().out

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["devices"][0]["disabled"] is True


def test_cli_legacy_bare_flags_still_mints(tmp_path, capsys):
    """The pre-subcommand CLI (`--registry ... --device-id ...`, no verb) must
    keep working for scripts written against the original interface."""
    registry_path = tmp_path / "tokens.json"

    assert main(["--registry", str(registry_path), "--device-id", "dev-1"]) == 0
    assert "Device token" in capsys.readouterr().out

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["devices"][0]["device_id"] == "dev-1"


def test_concurrent_mints_do_not_lose_updates(tmp_path):
    """Each mint is a read-modify-write cycle over the whole registry file;
    without a lock spanning the full cycle, two concurrent mints can each
    read the same pre-write state and the second write silently discards the
    first caller's new device entry."""
    registry_path = tmp_path / "tokens.json"
    device_ids = [f"dev-{i}" for i in range(20)]

    threads = [
        threading.Thread(target=mint, args=(registry_path, device_id, ""))
        for device_id in device_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert {d["device_id"] for d in registry["devices"]} == set(device_ids)
