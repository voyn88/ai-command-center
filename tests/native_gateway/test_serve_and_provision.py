"""TLS enforcement and token provisioning."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading

import pytest

from command_center import storage
from native_gateway import provision
from native_gateway.provision import mint, revoke
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


# --------------------------------------------------------------------------
# revoke
# --------------------------------------------------------------------------


def test_revoke_disables_device_without_deleting_it(tmp_path):
    registry_path = tmp_path / "tokens.json"
    token = mint(registry_path, "dev-1", "Owner iPhone")
    revoke(registry_path, "dev-1")

    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(stored["devices"]) == 1
    entry = stored["devices"][0]
    assert entry["disabled"] is True
    assert entry["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()


def test_revoke_rejects_unknown_device(tmp_path):
    registry_path = tmp_path / "tokens.json"
    mint(registry_path, "dev-1", "")
    with pytest.raises(SystemExit):
        revoke(registry_path, "dev-does-not-exist")


def test_revoke_rejects_missing_registry(tmp_path):
    registry_path = tmp_path / "does-not-exist" / "tokens.json"
    with pytest.raises(SystemExit):
        revoke(registry_path, "dev-1")


def test_revoke_is_idempotent(tmp_path):
    registry_path = tmp_path / "tokens.json"
    mint(registry_path, "dev-1", "")
    revoke(registry_path, "dev-1")
    revoke(registry_path, "dev-1")  # must not raise the second time

    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert stored["devices"][0]["disabled"] is True


# --------------------------------------------------------------------------
# CLI: legacy no-subcommand form vs explicit `mint`/`revoke`
# --------------------------------------------------------------------------


def test_cli_legacy_form_without_subcommand_still_mints(tmp_path, capsys):
    registry_path = tmp_path / "tokens.json"
    rc = provision.main(["--registry", str(registry_path), "--device-id", "dev-1"])
    assert rc == 0
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert stored["devices"][0]["device_id"] == "dev-1"
    assert "Device token" in capsys.readouterr().out


def test_cli_explicit_mint_subcommand(tmp_path, capsys):
    registry_path = tmp_path / "tokens.json"
    rc = provision.main(["mint", "--registry", str(registry_path), "--device-id", "dev-1"])
    assert rc == 0
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert stored["devices"][0]["device_id"] == "dev-1"


def test_cli_revoke_subcommand(tmp_path, capsys):
    registry_path = tmp_path / "tokens.json"
    provision.main(["mint", "--registry", str(registry_path), "--device-id", "dev-1"])
    rc = provision.main(["revoke", "--registry", str(registry_path), "--device-id", "dev-1"])
    assert rc == 0
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert stored["devices"][0]["disabled"] is True
    assert "Revoked" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Concurrency: the whole read-modify-write cycle must be race-safe, for
# mint-versus-mint *and* mint-versus-revoke (the race a prior review found
# unguarded: `revoke` checked `registry_path.exists()` before acquiring the
# lock, so a revoke landing exactly as the first mint creates the registry
# could report "registry not found" instead of waiting for that mint and
# revoking the device it just created).
# --------------------------------------------------------------------------


def test_concurrent_mint_of_two_different_devices_both_survive(tmp_path):
    registry_path = tmp_path / "tokens.json"
    start = threading.Barrier(2)
    errors: list[Exception] = []

    def run(device_id: str) -> None:
        start.wait()
        try:
            mint(registry_path, device_id, "")
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(d,)) for d in ("dev-a", "dev-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert {d["device_id"] for d in stored["devices"]} == {"dev-a", "dev-b"}


def test_revoke_waits_for_the_lock_instead_of_failing_fast_on_missing_registry(tmp_path):
    """Directly exercises the race the prior review flagged: a revoke that
    starts before the registry file exists must block on the registry lock
    (because a mint may be about to create it), never short-circuit on an
    out-of-lock `.exists()` check performed before any lock is taken."""
    registry_path = tmp_path / "tokens.json"
    lock_path = provision._lock_path(registry_path)
    errors: list[Exception] = []
    revoke_finished = threading.Event()

    def do_revoke() -> None:
        try:
            revoke(registry_path, "dev-1")
        except BaseException as exc:  # noqa: BLE001 - SystemExit included, surfaced via `errors`
            errors.append(exc)
        finally:
            revoke_finished.set()

    with storage.file_lock(lock_path, timeout=5):
        revoke_thread = threading.Thread(target=do_revoke)
        revoke_thread.start()
        # `revoke` must be blocked trying to acquire `lock_path`, not
        # already finished with a premature "registry not found".
        assert not revoke_finished.wait(timeout=0.5)

    revoke_thread.join(timeout=10)
    assert revoke_finished.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], SystemExit)
    assert "registry not found" in str(errors[0])


def test_concurrent_mint_and_revoke_of_same_device_never_corrupts_registry(tmp_path):
    registry_path = tmp_path / "tokens.json"
    start = threading.Barrier(2)
    errors: list[Exception] = []

    def do_mint() -> None:
        start.wait()
        try:
            mint(registry_path, "dev-1", "")
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    def do_revoke() -> None:
        start.wait()
        try:
            revoke(registry_path, "dev-1")
        except SystemExit:
            pass  # acceptable: revoke may legitimately run before the mint
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    threads = [threading.Thread(target=do_mint), threading.Thread(target=do_revoke)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert [d["device_id"] for d in stored["devices"]] == ["dev-1"]
