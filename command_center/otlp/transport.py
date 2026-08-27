"""The authenticated request itself: one POST to ``<endpoint>/v1/<signal>``.

This is the half of "otlp-ingest-auth" that a test can actually observe. The
policy in :mod:`command_center.otlp.config` decides that a credential exists;
this module decides how it travels, and what happens when the ingest rejects
it.

NO REDIRECTS, EVER
==================
``urllib``'s redirect handler copies the request headers onto the redirected
request. A 302 from the ingest -- or from anything that has taken over its
address -- would therefore hand AICC's bearer token to whatever host the
``Location`` names, over whatever scheme it names. That is a credential
disclosure triggered by a single response header, so the redirect handler is
not registered at all and a 3xx surfaces as a refusal.

The same opener registers no proxy handler. ``urllib.request`` otherwise reads
``http_proxy``/``https_proxy`` from the environment, which would let an
inherited variable silently route token-bearing telemetry through a third
party. The ingest here is an explicit endpoint on a private network; there is
no proxy to discover.

Only ``http`` and ``https`` handlers are registered, so a scheme that slipped
past configuration cannot reach ``file:`` or ``ftp:``.

WHY A 401 IS RETRIED EXACTLY ONCE, AND THEN IS FATAL
====================================================
This fleet rotates worker credentials on a schedule
(``voyn-aicc-credential-rotation.service``, SRV-03). A rotation that lands between reading
the token and the ingest verifying it produces a 401 that means "you used the
previous token half a second ago", not "you are not authorized". Retrying once
against a forced re-read of the credential file closes exactly that window.

A second 401 is not retried and is not swallowed. It raises
:class:`OtlpAuthRejected`, distinct from every transient failure, because the
two need opposite responses: a transient failure is dropped telemetry that the
next batch fixes, while a rejected credential is a permanent, silent loss of
observability that only an operator can repair. Collapsing them into one
"export failed" would leave the dashboard empty and nothing pointing at why --
the SRV-08 acceptance criterion ("alert fires on loss of a worker") is worth
nothing if the telemetry path can fail quietly.

Exceptions carry a bounded excerpt of the response body, passed through
:meth:`OtlpIngestConfig.redact` first. An ingest that echoes request headers
into an error body is not hypothetical, and AICC's own log is not a safe place
for its own credential.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from command_center.otlp.config import OtlpIngestConfig

__all__ = [
    "OtlpIngestError",
    "OtlpAuthRejected",
    "OtlpIngestUnavailable",
    "OtlpIngestRefused",
    "OtlpIngestClient",
    "DEFAULT_CONTENT_TYPE",
]

#: OTLP/HTTP defines protobuf and JSON encodings. AICC has no protobuf runtime
#: (and adding one to reach a collector that accepts JSON would be a
#: dependency bought for nothing), so the JSON encoding is the default.
DEFAULT_CONTENT_TYPE = "application/json"

#: Enough of an error body to identify the fault, bounded so a collector
#: returning an HTML error page cannot fill the log.
_BODY_EXCERPT_BYTES = 512

_USER_AGENT = "aicc-otlp/1"


class OtlpIngestError(RuntimeError):
    """Base class: an export attempt did not reach the ingest successfully."""


class OtlpAuthRejected(OtlpIngestError):
    """The ingest refused AICC's credential (401/403), twice.

    Permanent until an operator acts. Distinct from every transient failure
    precisely so a caller cannot treat it as one.
    """


class OtlpIngestUnavailable(OtlpIngestError):
    """A transient failure: network error, timeout, 429, or a 5xx.

    The batch is lost; the next one may well succeed.
    """


class OtlpIngestRefused(OtlpIngestError):
    """The ingest understood the request and rejected it (a 4xx, or a 3xx).

    A payload or routing fault, not a credential one -- retrying the same
    bytes cannot fix it.
    """


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener that speaks http(s) only, follows nothing, discovers nothing.

    Built by hand rather than via :func:`urllib.request.build_opener`, whose
    default handler set includes exactly the two handlers this must not have
    (redirects and proxies).
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        # With no redirect handler registered, a 3xx falls through to the
        # default error handler and is raised instead of followed.
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        # Any scheme without a handler raises rather than being ignored.
        urllib.request.UnknownHandler(),
    ):
        opener.add_handler(handler)
    return opener


class OtlpIngestClient:
    """Posts one encoded OTLP payload per call, authenticated."""

    __slots__ = ("_config", "_opener")

    def __init__(self, config: OtlpIngestConfig) -> None:
        self._config = config
        self._opener = _build_opener()

    @property
    def config(self) -> OtlpIngestConfig:
        return self._config

    def send(
        self,
        signal: str,
        payload: bytes,
        *,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> int:
        """Export ``payload`` for ``signal``; return the ingest's status code.

        Raises :class:`OtlpAuthRejected`, :class:`OtlpIngestRefused` or
        :class:`OtlpIngestUnavailable`. Never returns a non-2xx status: a
        caller that ignores the return value must still not mistake a
        rejection for a delivery.
        """
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError(
                "payload must be already-encoded bytes; encoding is the "
                "caller's decision and content_type must match it."
            )

        url = self._config.url_for(signal)
        try:
            return self._attempt(url, bytes(payload), content_type)
        except _Unauthorized:
            pass

        # One retry, against a forced re-read: see the module docstring. A
        # CredentialError here propagates -- if the file has become unreadable
        # during rotation, that is the fault worth reporting, not the 401.
        self._config.credential.reload()
        try:
            return self._attempt(url, bytes(payload), content_type)
        except _Unauthorized as exc:
            raise OtlpAuthRejected(
                f"OTLP ingest {self._config.redacted()} rejected AICC's "
                f"credential with HTTP {exc.status} after re-reading it. "
                f"Telemetry is not being delivered until this is fixed. "
                f"{exc.excerpt}"
            ) from exc.cause

    def _attempt(self, url: str, payload: bytes, content_type: str) -> int:
        request = urllib.request.Request(url, data=payload, method="POST")
        request.add_header("Content-Type", content_type)
        request.add_header("User-Agent", _USER_AGENT)
        for name, value in self._config.auth_headers().items():
            request.add_header(name, value)

        try:
            with self._opener.open(request, timeout=self._config.timeout_seconds) as r:
                # Drain: an undrained body leaves the connection in a state
                # the next request would have to tear down.
                r.read()
                return int(r.status)
        except urllib.error.HTTPError as exc:
            self._raise_for_status(exc)
            raise AssertionError("unreachable")  # pragma: no cover
        except urllib.error.URLError as exc:
            raise OtlpIngestUnavailable(
                f"OTLP ingest {self._config.redacted()} is unreachable: "
                f"{self._config.redact(str(exc.reason))}"
            ) from exc
        except TimeoutError as exc:
            raise OtlpIngestUnavailable(
                f"OTLP ingest {self._config.redacted()} did not answer within "
                f"{self._config.timeout_seconds}s."
            ) from exc

    def _raise_for_status(self, exc: urllib.error.HTTPError) -> None:
        status = int(exc.code)
        excerpt = self._excerpt(exc)

        if status in (401, 403):
            raise _Unauthorized(status, excerpt, exc)
        if status == 429 or 500 <= status < 600:
            raise OtlpIngestUnavailable(
                f"OTLP ingest {self._config.redacted()} answered HTTP {status}. "
                f"{excerpt}"
            ) from exc
        if 300 <= status < 400:
            raise OtlpIngestRefused(
                f"OTLP ingest {self._config.redacted()} answered HTTP {status} "
                f"(redirect). Redirects are never followed: the request carries "
                f"AICC's bearer token, and following one would disclose it to "
                f"the redirect target. Point {self._config.endpoint!r} at the "
                f"final address instead."
            ) from exc
        raise OtlpIngestRefused(
            f"OTLP ingest {self._config.redacted()} rejected the payload with "
            f"HTTP {status}. {excerpt}"
        ) from exc

    def _excerpt(self, exc: urllib.error.HTTPError) -> str:
        try:
            body = exc.read(_BODY_EXCERPT_BYTES)
        except Exception:  # pragma: no cover - a body that cannot be read
            return ""
        if not body:
            return ""
        text = body.decode("utf-8", errors="replace").strip()
        return f"Response: {self._config.redact(text)}"


class _Unauthorized(Exception):
    """Internal: a 401/403 that the retry may still resolve.

    Not part of the public exception hierarchy -- it must never escape
    :meth:`OtlpIngestClient.send`, which converts a second one into
    :class:`OtlpAuthRejected`.
    """

    def __init__(self, status: int, excerpt: str, cause: BaseException) -> None:
        super().__init__(status)
        self.status = status
        self.excerpt = excerpt
        self.cause = cause
