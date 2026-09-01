"""TLS enforcement and token provisioning."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time

import pytest

from native_gateway import provision
from native_gateway.auth import DeviceRegistry
from native_gateway.provision import RegistryCorruptError, main, mint, revoke
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


def test_revoke_disables_device_without_deleting_it(tmp_path):
    registry_path = tmp_path / "tokens.json"
    token = mint(registry_path, "dev-1", "Owner phone")
    revoke(registry_path, "dev-1")

    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    entry = stored["devices"][0]
    assert entry["device_id"] == "dev-1"
    assert entry["disabled"] is True

    assert DeviceRegistry(registry_path).authenticate(token) is None


def test_revoke_unknown_device_raises(tmp_path):
    registry_path = tmp_path / "tokens.json"
    mint(registry_path, "dev-1", "")
    with pytest.raises(SystemExit):
        revoke(registry_path, "no-such-device")


def test_revoke_against_missing_registry_raises(tmp_path):
    registry_path = tmp_path / "never-created.json"
    with pytest.raises(SystemExit):
        revoke(registry_path, "dev-1")


def test_mint_refuses_to_overwrite_corrupt_registry(tmp_path):
    registry_path = tmp_path / "tokens.json"
    registry_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(RegistryCorruptError):
        mint(registry_path, "dev-1", "")
    # The corrupt file must survive untouched -- mint must not have replaced it.
    assert registry_path.read_text(encoding="utf-8") == "{not valid json"


def test_revoke_refuses_corrupt_registry(tmp_path):
    registry_path = tmp_path / "tokens.json"
    registry_path.write_text(json.dumps({"devices": "not-a-list"}), encoding="utf-8")
    with pytest.raises(RegistryCorruptError):
        revoke(registry_path, "dev-1")


def test_cli_legacy_mint_form_without_subcommand(tmp_path, capsys):
    registry_path = tmp_path / "tokens.json"
    rc = main(
        ["--registry", str(registry_path), "--device-id", "dev-1", "--label", "phone"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Device token" in out
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert stored["devices"][0]["device_id"] == "dev-1"


def test_cli_revoke_subcommand(tmp_path, capsys):
    registry_path = tmp_path / "tokens.json"
    mint(registry_path, "dev-1", "")
    rc = main(["revoke", "--registry", str(registry_path), "--device-id", "dev-1"])
    assert rc == 0
    assert "Revoked device: dev-1" in capsys.readouterr().out
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert stored["devices"][0]["disabled"] is True


def _run_collecting_errors(target, errors):
    try:
        target()
    except SystemExit:
        pass  # expected for the duplicate-device / not-found losers of a race
    except Exception as exc:  # noqa: BLE001 - surfaced to the test thread below
        errors.append(exc)


def test_concurrent_mints_do_not_lose_devices(tmp_path):
    registry_path = tmp_path / "tokens.json"
    device_ids = [f"dev-{i}" for i in range(12)]
    errors: list[Exception] = []
    threads = [
        threading.Thread(
            target=_run_collecting_errors,
            args=(lambda d=device_id: mint(registry_path, d, ""), errors),
        )
        for device_id in device_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"worker threads raised: {errors}"
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert {d["device_id"] for d in stored["devices"]} == set(device_ids)


def test_concurrent_mint_and_revoke_of_the_same_new_device(tmp_path, monkeypatch):
    """A revoke racing a device's very first mint -- started while mint is
    still inside the locked critical section, before the registry file has
    even been written -- must block on the lock and then find and revoke the
    newly minted device, not observe "no registry yet" and report it unknown.
    That was the exact bug: the existence check ran before lock acquisition."""
    registry_path = tmp_path / "tokens.json"
    errors: list[Exception] = []
    mint_holding_lock = threading.Event()
    release_mint = threading.Event()
    real_write_registry = provision._write_registry

    def slow_write_registry(path, registry):
        mint_holding_lock.set()
        assert release_mint.wait(timeout=5), "test failed to release mint in time"
        real_write_registry(path, registry)

    monkeypatch.setattr(provision, "_write_registry", slow_write_registry)

    mint_thread = threading.Thread(
        target=_run_collecting_errors,
        args=(lambda: mint(registry_path, "dev-race", ""), errors),
    )
    mint_thread.start()
    assert mint_holding_lock.wait(timeout=5), "mint never reached its write"

    revoke_thread = threading.Thread(
        target=_run_collecting_errors,
        args=(lambda: revoke(registry_path, "dev-race"), errors),
    )
    revoke_thread.start()
    # Give revoke a chance to reach (and block on) the lock mint still holds.
    time.sleep(0.2)
    release_mint.set()

    mint_thread.join(timeout=5)
    revoke_thread.join(timeout=5)

    assert not errors, f"worker threads raised: {errors}"
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(stored["devices"]) == 1
    assert stored["devices"][0]["device_id"] == "dev-race"
    assert stored["devices"][0]["disabled"] is True
