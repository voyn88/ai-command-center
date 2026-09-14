"""``AICC_PLATFORM_URL`` scheme validation.

The identity authority receives the caller's live bearer credential on every
request (see ``identity.py``'s module docstring for why there is no local
verification path). A scheme that does not encrypt the transport — plain
``http`` to anything but the operator's own machine — would ship that
credential in cleartext to whatever sits on the network path. These tests
pin the one exception (loopback ``http``, the supported local-dev shape) and
prove everything else is refused loudly rather than silently used.
"""

from __future__ import annotations

import pytest

from command_center.http_auth import identity


@pytest.fixture
def _clear_env(monkeypatch):
    monkeypatch.delenv(identity.PLATFORM_URL_ENV, raising=False)


def test_unconfigured_url_is_none_not_an_error(_clear_env):
    assert identity.platform_base_url() is None


@pytest.mark.parametrize(
    "url",
    [
        "https://platform.internal",
        "https://platform.internal/",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://[::1]:8080",
    ],
)
def test_accepted_urls_pass_through_normalized(_clear_env, monkeypatch, url):
    monkeypatch.setenv(identity.PLATFORM_URL_ENV, url)
    assert identity.platform_base_url() == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://platform.internal",  # plain http to a routable host
        "http://203.0.113.5",  # plain http to a routable IP
        "ftp://platform.internal",
        "file:///etc/passwd",
        "platform.internal",  # no scheme at all
    ],
)
def test_unsafe_urls_are_rejected_with_a_config_error(_clear_env, monkeypatch, url):
    monkeypatch.setenv(identity.PLATFORM_URL_ENV, url)
    with pytest.raises(identity.PlatformURLError):
        identity.platform_base_url()


def test_config_error_is_not_platform_unavailable(_clear_env, monkeypatch):
    """The two must stay distinguishable: one is transient (retry may help),
    the other is a standing misconfiguration (retry never helps)."""
    monkeypatch.setenv(identity.PLATFORM_URL_ENV, "http://platform.internal")
    with pytest.raises(identity.PlatformURLError) as excinfo:
        identity.platform_base_url()
    assert not isinstance(excinfo.value, identity.PlatformUnavailable)
