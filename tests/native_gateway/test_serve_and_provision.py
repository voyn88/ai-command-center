"""TLS enforcement and token provisioning."""

from __future__ import annotations

import hashlib
import os
import json
import stat

import pytest

from native_gateway.provision import mint
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
