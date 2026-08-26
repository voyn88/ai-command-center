"""Unit tests for the storage-free model-registry policy
(``command_center.models_registry`` — auto-select, the sensitive guard and the
stub downloader). Pure functions over plain mappings; no db, no HTTP.
"""

from __future__ import annotations

import pytest

from command_center.models_registry import (
    SensitiveModelRoutingError,
    StubDownloader,
    assert_routing_allowed,
    auto_select,
)
from command_center.models_registry.downloader import DownloadFailed


def _m(**kw):
    base = {"id": "m", "kind": "external", "status": "available"}
    base.update(kw)
    return base


# --- auto_select ----------------------------------------------------------


def test_auto_select_prefers_local_over_external() -> None:
    external = _m(id="e", kind="external", cost=0.0)
    local = _m(id="l", kind="local", cost=5.0)
    # local wins even though it is more expensive — local tier is preferred.
    assert auto_select([external, local])["id"] == "l"


def test_auto_select_breaks_ties_by_cost_then_quality_then_latency() -> None:
    a = _m(id="a", kind="local", cost=1.0, quality=0.5, latency_ms=100)
    b = _m(id="b", kind="local", cost=1.0, quality=0.9, latency_ms=100)
    c = _m(id="c", kind="local", cost=0.5, quality=0.1, latency_ms=900)
    # cheapest first (c), even with the worst quality/latency
    assert auto_select([a, b, c])["id"] == "c"
    # among equal cost, higher quality wins (b over a)
    assert auto_select([a, b])["id"] == "b"


def test_auto_select_skips_unusable_status() -> None:
    downloading = _m(id="d", kind="local", cost=0.0, status="downloading")
    available = _m(id="a", kind="external", cost=9.0, status="available")
    # the cheap local model is still downloading → the available external is chosen
    assert auto_select([downloading, available])["id"] == "a"
    assert auto_select([downloading]) is None


def test_auto_select_sensitive_excludes_external() -> None:
    external = _m(id="e", kind="external", cost=0.0)
    local = _m(id="l", kind="local", cost=9.0)
    assert auto_select([external, local], sensitive=True)["id"] == "l"
    # nothing local available → None rather than falling back to external
    assert auto_select([external], sensitive=True) is None


def test_auto_select_empty_is_none() -> None:
    assert auto_select([]) is None


# --- sensitive guard ------------------------------------------------------


def test_guard_blocks_external_for_sensitive_context() -> None:
    with pytest.raises(SensitiveModelRoutingError):
        assert_routing_allowed(_m(kind="external"), sensitive=True)


def test_guard_allows_local_and_non_sensitive() -> None:
    assert_routing_allowed(_m(kind="local"), sensitive=True)  # no raise
    assert_routing_allowed(_m(kind="external"), sensitive=False)  # no raise


# --- stub downloader ------------------------------------------------------


def test_stub_downloader_emits_monotonic_progress_to_100() -> None:
    ticks = list(StubDownloader(steps=4).fetch(model_id="m", provenance=None))
    assert [t.percent for t in ticks] == [25, 50, 75, 100]


def test_stub_downloader_can_fail_deterministically() -> None:
    gen = StubDownloader(steps=4, fail_at=50).fetch(model_id="m", provenance=None)
    assert next(gen).percent == 25
    with pytest.raises(DownloadFailed):
        next(gen)
