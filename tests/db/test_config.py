"""Configuration is the security boundary, so it is tested as one.

These tests need no database: they assert that a misconfigured environment
fails at load time rather than producing a connection to somewhere unintended.
"""

from __future__ import annotations

import pytest

from command_center.db.config import ConfigError, load_config

_STRONG_PASSWORD = "fixture-Not-A-Real-Password-7Xq2"
"""A fixture password that is obviously not one, and derived from nothing.

The original shared its whole 16-character tail with a password in actual use
on a developer machine — independent acceptance spotted the overlap. A test
constant that is a real secret with a few characters changed is a real secret:
it leaks the shape, the alphabet and most of the material, and it invites the
next person to edit rather than regenerate.

My first replacement was 24 random characters, and the repository's secret scan
flagged it — correctly. A high-entropy string in a source file is
indistinguishable from a leaked credential to anything but a human, and
answering that by widening the scanner's baseline would train the baseline to
absorb exactly what it exists to catch.

So the shape is neither a real secret nor a random one: it reads as a fixture
at a glance, it satisfies the strength rule this test exercises, and it is
stable, so the baseline does not churn whenever someone touches this line.
"""


def _env(**overrides: str) -> dict[str, str]:
    base = {
        "AICC_PG_HOST": "127.0.0.1",
        "AICC_PG_DB": "aicc",
        "AICC_PG_USER": "aicc_app",
        "AICC_PG_PASSWORD": _STRONG_PASSWORD,
        "AICC_PG_SSLMODE": "prefer",  # allowed only because the host is loopback
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_loopback_defaults_load() -> None:
    config = load_config(_env())
    assert config.port == 5432
    assert config.is_loopback


@pytest.mark.parametrize(
    "missing", ["AICC_PG_HOST", "AICC_PG_DB", "AICC_PG_USER", "AICC_PG_PASSWORD"]
)
def test_required_settings_have_no_default(missing: str) -> None:
    env = _env()
    del env[missing]
    with pytest.raises(ConfigError, match=missing):
        load_config(env)


def test_empty_value_is_not_a_value() -> None:
    with pytest.raises(ConfigError, match="AICC_PG_PASSWORD"):
        load_config(_env(AICC_PG_PASSWORD="   "))


@pytest.mark.parametrize("password", ["postgres", "changeme", "PASSWORD", "aicc"])
def test_well_known_passwords_are_rejected(password: str) -> None:
    with pytest.raises(ConfigError, match="well-known"):
        load_config(_env(AICC_PG_PASSWORD=password))


def test_password_equal_to_user_is_rejected() -> None:
    with pytest.raises(ConfigError, match="must not equal"):
        load_config(_env(AICC_PG_USER="verylongrolename", AICC_PG_PASSWORD="verylongrolename"))


def test_short_password_is_rejected() -> None:
    with pytest.raises(ConfigError, match="at least 16"):
        load_config(_env(AICC_PG_PASSWORD="Kq7vRt2wX"))


def test_non_verifying_sslmode_is_rejected_off_loopback() -> None:
    """`require` encrypts but does not authenticate the server."""
    with pytest.raises(ConfigError, match="does not verify"):
        load_config(_env(AICC_PG_HOST="db.internal", AICC_PG_SSLMODE="require"))


def test_verifying_sslmode_requires_a_pinned_root() -> None:
    with pytest.raises(ConfigError, match="AICC_PG_SSLROOTCERT"):
        load_config(_env(AICC_PG_HOST="db.internal", AICC_PG_SSLMODE="verify-full"))


def test_remote_host_with_verified_tls_loads() -> None:
    config = load_config(
        _env(
            AICC_PG_HOST="db.internal",
            AICC_PG_SSLMODE="verify-full",
            AICC_PG_SSLROOTCERT="/etc/ssl/certs/internal-ca.pem",
        )
    )
    assert config.sslmode == "verify-full"
    assert not config.is_loopback


def test_unknown_sslmode_is_rejected() -> None:
    with pytest.raises(ConfigError, match="not a libpq sslmode"):
        load_config(_env(AICC_PG_SSLMODE="verify_full"))


@pytest.mark.parametrize("port", ["0", "70000", "not-a-number"])
def test_invalid_port_is_rejected(port: str) -> None:
    with pytest.raises(ConfigError, match="AICC_PG_PORT"):
        load_config(_env(AICC_PG_PORT=port))


def test_pool_max_below_min_is_rejected() -> None:
    with pytest.raises(ConfigError, match="below"):
        load_config(_env(AICC_PG_POOL_MIN="5", AICC_PG_POOL_MAX="2"))


def test_redacted_never_contains_the_password() -> None:
    config = load_config(_env())
    assert _STRONG_PASSWORD not in config.redacted()
    assert "aicc_app@127.0.0.1:5432/aicc" in config.redacted()


def test_conninfo_carries_credentials_and_statement_timeout() -> None:
    config = load_config(_env())
    conninfo = config.conninfo()
    assert _STRONG_PASSWORD in conninfo
    assert "statement_timeout=30000" in conninfo


def test_conninfo_quotes_special_characters() -> None:
    """A password with a space or quote must not split the connection string."""
    tricky = "aB3 'space'-and-quote-Xy9Zq"
    conninfo = load_config(_env(AICC_PG_PASSWORD=tricky)).conninfo()
    assert "password='aB3 \\'space\\'-and-quote-Xy9Zq'" in conninfo
