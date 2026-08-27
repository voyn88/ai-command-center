"""The fail-closed contract: export is off, or export is authenticated.

The tests are grouped by the three ways a deployment can be wrong -- switched
half-on, pointed somewhere unsafe, or leaking the secret it was given.
"""

from __future__ import annotations

import ast
import os
import pathlib

import pytest

from command_center.otlp import config as otlp_config
from command_center.otlp.config import (
    ENDPOINT_ENV,
    TIMEOUT_ENV,
    TOKEN_FILE_ENV,
    ConfigError,
    load_config,
)

TOKEN = "s3cr3t-otlp-token-value"


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "otlp.token"
    path.write_text(TOKEN, encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path)


def env(endpoint=None, token=None, **extra):
    values = {}
    if endpoint is not None:
        values[ENDPOINT_ENV] = endpoint
    if token is not None:
        values[TOKEN_FILE_ENV] = token
    values.update(extra)
    return values


# -- off, on, and nothing in between -------------------------------------


def test_no_configuration_means_export_is_off(token_file):
    assert load_config({}) is None


def test_an_empty_endpoint_means_export_is_off():
    assert load_config({ENDPOINT_ENV: "   "}) is None


def test_an_endpoint_without_a_credential_refuses(token_file):
    """The whole point: there is no anonymous export path."""
    with pytest.raises(ConfigError, match="does not export telemetry anonymously"):
        load_config(env(endpoint="https://collector.internal:4318"))


def test_a_credential_without_an_endpoint_refuses(token_file):
    """Half-configured must be loud: the operator is expecting telemetry."""
    with pytest.raises(ConfigError, match="is set but"):
        load_config(env(token=token_file))


def test_a_complete_configuration_loads(token_file):
    loaded = load_config(env("https://collector.internal:4318", token_file))
    assert loaded is not None
    assert loaded.endpoint == "https://collector.internal:4318"
    assert loaded.credential.token == TOKEN
    assert loaded.timeout_seconds == 10.0


def test_an_unreadable_credential_refuses_at_load_not_at_first_export(tmp_path):
    with pytest.raises(ConfigError, match="cannot be read"):
        load_config(env("https://collector.internal:4318", str(tmp_path / "absent")))


def test_a_world_readable_credential_refuses_at_load(tmp_path):
    path = tmp_path / "otlp.token"
    path.write_text(TOKEN, encoding="utf-8")
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError, match="readable by group or others"):
        load_config(env("https://collector.internal:4318", str(path)))


def test_there_is_no_switch_that_disables_authentication():
    """A guard against the obvious future "just for staging" regression.

    Docstrings are stripped before the check because this module's prose
    names ``AICC_OTLP_INSECURE`` precisely to record that it does not exist.
    The assertion is about executable code: no such flag may be read.
    """
    tree = ast.parse(pathlib.Path(otlp_config.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]

    code = ast.unparse(tree).upper()
    for escape in ("INSECURE", "SKIP_AUTH", "NO_AUTH", "ALLOW_ANONYMOUS", "VERIFY=FALSE"):
        assert escape not in code, escape

    # And the only environment this module reads is the declared three.
    read = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("AICC_")
    }
    assert read == {ENDPOINT_ENV, TOKEN_FILE_ENV, TIMEOUT_ENV}


# -- where it is allowed to point ----------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:4318",
        "http://localhost:4318",
        "http://[::1]:4318",
    ],
)
def test_plaintext_is_allowed_only_on_loopback(endpoint, token_file):
    """Matches the deployed topology: an SSH tunnel terminating on 127.0.0.1."""
    loaded = load_config(env(endpoint, token_file))
    assert loaded is not None and loaded.endpoint == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://collector.internal:4318",
        "http://10.20.0.2:4318",
        "http://192.168.1.9:4318",
    ],
)
def test_plaintext_to_a_routed_host_refuses(endpoint, token_file):
    """A bearer token on the wire is replayable by anyone who reads it."""
    with pytest.raises(ConfigError, match="plaintext http"):
        load_config(env(endpoint, token_file))


def test_a_hostname_is_never_treated_as_loopback_by_resolution(token_file):
    """DNS can change under a running process; configuration cannot."""
    with pytest.raises(ConfigError, match="plaintext http"):
        load_config(env("http://loopback.example.test:4318", token_file))


def test_loopback_still_requires_a_credential(token_file):
    """The exemption relaxes transport confidentiality, never authentication."""
    with pytest.raises(ConfigError, match="does not export telemetry anonymously"):
        load_config(env(endpoint="http://127.0.0.1:4318"))


@pytest.mark.parametrize(
    "endpoint",
    ["ftp://collector:4318", "file:///tmp/x", "collector.internal:4318", "  "],
)
def test_a_non_http_endpoint_refuses(endpoint, token_file):
    values = env(endpoint, token_file)
    if not endpoint.strip():
        # An empty endpoint with a credential set is the half-configured case.
        with pytest.raises(ConfigError, match="is set but"):
            load_config(values)
        return
    with pytest.raises(ConfigError, match="http or https"):
        load_config(values)


def test_userinfo_in_the_endpoint_refuses(token_file):
    """urllib would send it as a competing, ambient Authorization header."""
    with pytest.raises(ConfigError, match="must not carry userinfo"):
        load_config(env("https://user:pw@collector.internal:4318", token_file))


@pytest.mark.parametrize("suffix", ["?tenant=1", "#frag"])
def test_a_query_or_fragment_in_the_endpoint_refuses(suffix, token_file):
    with pytest.raises(ConfigError, match="bare base URL"):
        load_config(env(f"https://collector.internal:4318{suffix}", token_file))


def test_a_trailing_slash_is_normalized_away(token_file):
    loaded = load_config(env("https://collector.internal:4318/", token_file))
    assert loaded is not None
    assert loaded.url_for("traces") == "https://collector.internal:4318/v1/traces"


def test_a_base_path_is_preserved(token_file):
    """Collectors behind a reverse proxy live under a prefix."""
    loaded = load_config(env("https://gw.internal/otlp", token_file))
    assert loaded is not None
    assert loaded.url_for("logs") == "https://gw.internal/otlp/v1/logs"


def test_an_endpoint_without_a_host_refuses(token_file):
    with pytest.raises(ConfigError, match="no host"):
        load_config(env("https:///v1/traces", token_file))


# -- signals -------------------------------------------------------------


@pytest.mark.parametrize("signal", ["traces", "metrics", "logs"])
def test_every_otlp_signal_builds_a_url(signal, token_file):
    loaded = load_config(env("https://collector.internal:4318", token_file))
    assert loaded is not None
    assert loaded.url_for(signal).endswith(f"/v1/{signal}")


@pytest.mark.parametrize("signal", ["trace", "../../admin", "", "TRACES"])
def test_a_signal_outside_the_closed_set_refuses(signal, token_file):
    """The name is interpolated into a path; it cannot be caller-supplied."""
    loaded = load_config(env("https://collector.internal:4318", token_file))
    assert loaded is not None
    with pytest.raises(ConfigError, match="not an OTLP signal"):
        loaded.url_for(signal)


# -- the timeout ---------------------------------------------------------


def test_the_timeout_is_read_from_the_environment(token_file):
    loaded = load_config(
        env("https://collector.internal:4318", token_file, **{TIMEOUT_ENV: "2.5"})
    )
    assert loaded is not None and loaded.timeout_seconds == 2.5


@pytest.mark.parametrize("bad", ["nope", ""])
def test_a_non_numeric_timeout_refuses_or_defaults(bad, token_file):
    values = env("https://collector.internal:4318", token_file, **{TIMEOUT_ENV: bad})
    if bad == "":
        assert load_config(values).timeout_seconds == 10.0
        return
    with pytest.raises(ConfigError, match="is not a number"):
        load_config(values)


@pytest.mark.parametrize("bad", ["0", "0.01", "121", "-5"])
def test_a_timeout_outside_the_band_refuses(bad, token_file):
    with pytest.raises(ConfigError, match="outside"):
        load_config(
            env("https://collector.internal:4318", token_file, **{TIMEOUT_ENV: bad})
        )


# -- the secret never leaves by accident ---------------------------------


def test_repr_and_redacted_never_carry_the_token(token_file):
    loaded = load_config(env("https://collector.internal:4318", token_file))
    assert loaded is not None
    assert TOKEN not in repr(loaded)
    assert TOKEN not in loaded.redacted()
    assert "collector.internal" in loaded.redacted()


def test_the_dataclass_repr_is_overridden(token_file):
    """A generated repr would print the credential; that is how secrets leak."""
    loaded = load_config(env("https://collector.internal:4318", token_file))
    assert repr(loaded).startswith("OtlpIngestConfig(https://")


def test_the_auth_header_is_a_bearer_token_rebuilt_per_call(token_file):
    loaded = load_config(env("https://collector.internal:4318", token_file))
    assert loaded is not None
    assert loaded.auth_headers() == {"Authorization": f"Bearer {TOKEN}"}

    with open(token_file, "w", encoding="utf-8") as handle:
        handle.write("rotated-token-of-a-different-length")
    assert loaded.auth_headers() == {
        "Authorization": "Bearer rotated-token-of-a-different-length"
    }


def test_load_config_reads_os_environ_when_given_nothing(monkeypatch, token_file):
    monkeypatch.setenv(ENDPOINT_ENV, "https://collector.internal:4318")
    monkeypatch.setenv(TOKEN_FILE_ENV, token_file)
    loaded = load_config()
    assert loaded is not None and loaded.endpoint == "https://collector.internal:4318"


def test_the_default_environment_has_export_off(monkeypatch):
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(TOKEN_FILE_ENV, raising=False)
    assert load_config() is None
