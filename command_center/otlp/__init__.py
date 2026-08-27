"""Authenticated OTLP export (VOYN-W0-AICC-SRV-08-OTLP-AUTH).

SRV-08 wants metrics, logs and traces off the control host. Shipping them
means AICC writes into an observability store, and this package is the
guarantee that it can only do so as an authenticated writer.

Read the modules in this order:

``config``
    Whether export is on at all, where it points, and the rule that switching
    it on without a credential is a startup failure rather than a warning.
``credential``
    Where the bearer token comes from (a file, not the environment) and why it
    is re-read rather than captured once.
``transport``
    The request: no redirects, no proxies, and a 401 that is retried once
    against a rotated credential and then raised loudly.

Importing this package pulls in nothing outside the standard library, so a
process that never exports telemetry pays nothing for its presence -- and with
``AICC_OTLP_ENDPOINT`` unset, :func:`load_config` returns ``None`` and no
client is ever constructed.
"""

from __future__ import annotations

from command_center.otlp.config import (
    ENDPOINT_ENV,
    SIGNALS,
    TIMEOUT_ENV,
    TOKEN_FILE_ENV,
    ConfigError,
    OtlpIngestConfig,
    load_config,
)
from command_center.otlp.credential import Credential, CredentialError
from command_center.otlp.transport import (
    DEFAULT_CONTENT_TYPE,
    OtlpAuthRejected,
    OtlpIngestClient,
    OtlpIngestError,
    OtlpIngestRefused,
    OtlpIngestUnavailable,
)

__all__ = [
    "ConfigError",
    "Credential",
    "CredentialError",
    "DEFAULT_CONTENT_TYPE",
    "ENDPOINT_ENV",
    "OtlpAuthRejected",
    "OtlpIngestClient",
    "OtlpIngestConfig",
    "OtlpIngestError",
    "OtlpIngestRefused",
    "OtlpIngestUnavailable",
    "SIGNALS",
    "TIMEOUT_ENV",
    "TOKEN_FILE_ENV",
    "load_config",
]
