"""`python -m command_center.otlp check`: the exit code is the contract.

An operator wires this into a deploy check or reads it off a terminal at 3am,
so each failure must be distinguishable without parsing prose. These tests
pin the codes against a real loopback ingest.
"""

from __future__ import annotations

import os

import pytest

from command_center.otlp import cli
from command_center.otlp.config import ENDPOINT_ENV, TIMEOUT_ENV, TOKEN_FILE_ENV

from tests.otlp.test_transport import TOKEN, _Ingest

pytestmark = pytest.mark.usefixtures("clean_otlp_env")


@pytest.fixture
def clean_otlp_env(monkeypatch):
    for name in (ENDPOINT_ENV, TOKEN_FILE_ENV, TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ingest():
    server = _Ingest()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def token_path(tmp_path):
    path = tmp_path / "otlp.token"
    path.write_text(TOKEN, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def configure(monkeypatch, endpoint, token_path):
    monkeypatch.setenv(ENDPOINT_ENV, endpoint)
    monkeypatch.setenv(TOKEN_FILE_ENV, str(token_path))


def test_export_off_is_its_own_exit_code(capsys):
    assert cli.main(["check"]) == cli.EXIT_DISABLED
    assert "export is off" in capsys.readouterr().out


def test_an_accepted_credential_exits_zero(monkeypatch, ingest, token_path, capsys):
    configure(monkeypatch, ingest.url, token_path)
    assert cli.main(["check"]) == cli.EXIT_OK
    assert "accepted" in capsys.readouterr().out
    assert ingest.received[-1].header("Authorization") == f"Bearer {TOKEN}"


def test_the_probe_payload_carries_no_telemetry(monkeypatch, ingest, token_path):
    """Safe to run against production: it creates nothing."""
    configure(monkeypatch, ingest.url, token_path)
    cli.main(["check"])
    assert ingest.received[-1].body == b'{"resourceSpans":[]}'


@pytest.mark.parametrize("signal", ["traces", "metrics", "logs"])
def test_each_signal_can_be_probed(monkeypatch, ingest, token_path, signal):
    configure(monkeypatch, ingest.url, token_path)
    assert cli.main(["check", "--signal", signal]) == cli.EXIT_OK
    assert ingest.received[-1].path == f"/v1/{signal}"
    assert ingest.received[-1].body == cli.EMPTY_PAYLOAD[signal]


def test_a_rejected_credential_exits_one(monkeypatch, ingest, token_path, capsys):
    ingest.responder = lambda request: (401, b"nope", {})
    configure(monkeypatch, ingest.url, token_path)
    assert cli.main(["check"]) == cli.EXIT_REJECTED
    assert "rejected" in capsys.readouterr().out


def test_an_unreachable_ingest_exits_two(monkeypatch, ingest, token_path, capsys):
    url = ingest.url
    ingest.close()
    configure(monkeypatch, url, token_path)
    assert cli.main(["check"]) == cli.EXIT_UNREACHABLE
    assert "unreachable" in capsys.readouterr().out


def test_an_endpoint_that_is_not_a_collector_exits_two(
    monkeypatch, ingest, token_path, capsys
):
    """A 404 on an empty payload is a wrong address, not a bad credential."""
    ingest.responder = lambda request: (404, b"not found", {})
    configure(monkeypatch, ingest.url, token_path)
    assert cli.main(["check"]) == cli.EXIT_UNREACHABLE
    assert "refused" in capsys.readouterr().out


def test_a_bad_configuration_exits_three(monkeypatch, token_path, capsys):
    monkeypatch.setenv(ENDPOINT_ENV, "http://collector.internal:4318")
    monkeypatch.setenv(TOKEN_FILE_ENV, str(token_path))
    assert cli.main(["check"]) == cli.EXIT_MISCONFIGURED
    assert "misconfigured" in capsys.readouterr().out


def test_an_unreadable_credential_exits_three(monkeypatch, ingest, tmp_path, capsys):
    monkeypatch.setenv(ENDPOINT_ENV, ingest.url)
    monkeypatch.setenv(TOKEN_FILE_ENV, str(tmp_path / "absent"))
    assert cli.main(["check"]) == cli.EXIT_MISCONFIGURED
    assert "misconfigured" in capsys.readouterr().out


def test_no_output_ever_carries_the_token(monkeypatch, ingest, token_path, capsys):
    ingest.responder = lambda request: (
        401,
        f'saw {request.header("Authorization")}'.encode(),
        {},
    )
    configure(monkeypatch, ingest.url, token_path)
    cli.main(["check"])
    assert TOKEN not in capsys.readouterr().out


def test_the_exit_codes_are_distinct():
    codes = [
        cli.EXIT_OK,
        cli.EXIT_REJECTED,
        cli.EXIT_UNREACHABLE,
        cli.EXIT_MISCONFIGURED,
        cli.EXIT_DISABLED,
    ]
    assert len(set(codes)) == len(codes)


def test_an_unknown_signal_is_refused_by_the_parser(capsys):
    with pytest.raises(SystemExit):
        cli.main(["check", "--signal", "../../admin"])
