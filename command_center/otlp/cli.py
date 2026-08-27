"""`python -m command_center.otlp check` — prove the ingest credential works.

An authentication path nobody can test is one nobody trusts. Until a span
exporter exists to consume this package (a separate SRV-08 slice), an operator
configuring ``AICC_OTLP_*`` has no way to find out whether the token is
accepted except to wait and see whether traces appear -- which is precisely the
silent-failure shape this package exists to remove.

``check`` posts an OTLP request whose payload is an empty collection. That is a
well-formed request under the OTLP/HTTP spec and it creates no telemetry, so it
is safe to run against production: it exercises the credential and nothing
else. The exit codes are distinct because the three failures need three
different people::

    0  the ingest accepted the credential
    1  the ingest rejected it            -- rotate or re-grant the token
    2  the ingest could not be reached   -- network/collector problem
    3  the configuration is invalid      -- fix the environment
    4  export is switched off            -- nothing to check

Run it the way the worker will run: with the same environment and the same
user, because a credential file readable by root and not by the service
account passes every check except the one that matters.

    AICC_OTLP_ENDPOINT=https://collector.internal:4318 \\
    AICC_OTLP_TOKEN_FILE=/etc/voyn/secrets/otlp_token \\
    python -m command_center.otlp check
"""

from __future__ import annotations

import argparse

from command_center.otlp.config import SIGNALS, ConfigError, load_config
from command_center.otlp.credential import CredentialError
from command_center.otlp.transport import (
    OtlpAuthRejected,
    OtlpIngestClient,
    OtlpIngestRefused,
    OtlpIngestUnavailable,
)

__all__ = ["build_parser", "main", "EMPTY_PAYLOAD"]

#: The empty collection for each signal: a valid OTLP/HTTP body that carries
#: no spans, metrics or log records, so a probe cannot pollute the store.
EMPTY_PAYLOAD = {
    "traces": b'{"resourceSpans":[]}',
    "metrics": b'{"resourceMetrics":[]}',
    "logs": b'{"resourceLogs":[]}',
}

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_UNREACHABLE = 2
EXIT_MISCONFIGURED = 3
EXIT_DISABLED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m command_center.otlp")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser(
        "check",
        help="Post an empty OTLP payload and report whether the ingest "
        "accepted AICC's credential.",
    )
    check.add_argument(
        "--signal",
        default="traces",
        choices=sorted(SIGNALS),
        help="Which OTLP signal endpoint to probe (default: traces).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return _check(args.signal)
    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


def _check(signal: str) -> int:
    try:
        config = load_config()
    except (ConfigError, CredentialError) as exc:
        print(f"misconfigured: {exc}")
        return EXIT_MISCONFIGURED

    if config is None:
        print(
            "export is off: AICC_OTLP_ENDPOINT is unset, so no telemetry "
            "leaves this host and there is no credential to check."
        )
        return EXIT_DISABLED

    print(f"checking {config.redacted()} ...")
    try:
        status = OtlpIngestClient(config).send(signal, EMPTY_PAYLOAD[signal])
    except OtlpAuthRejected as exc:
        print(f"rejected: {exc}")
        return EXIT_REJECTED
    except OtlpIngestUnavailable as exc:
        print(f"unreachable: {exc}")
        return EXIT_UNREACHABLE
    except OtlpIngestRefused as exc:
        # The credential was not the problem: the ingest answered, and a 4xx
        # on an empty payload means the endpoint is not an OTLP receiver.
        print(f"refused: {exc}")
        return EXIT_UNREACHABLE
    except CredentialError as exc:
        print(f"misconfigured: {exc}")
        return EXIT_MISCONFIGURED

    print(f"accepted: the ingest answered HTTP {status} to an authenticated probe.")
    return EXIT_OK
