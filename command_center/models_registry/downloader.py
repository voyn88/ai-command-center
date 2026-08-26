"""The injectable downloader interface a local-model download drives.

A local model's *lifecycle* (available → downloading → installed, with a 0..100
progress) is real and persisted (see
:mod:`command_center.runtime.db.model_registry`). The actual byte transfer is
abstracted behind :class:`Downloader` so it can be swapped: production wires a
real network fetcher, tests inject :class:`StubDownloader`, which emits a
deterministic sequence of progress ticks and never touches the network.

The service consumes the yielded :class:`DownloadProgress` values one at a time,
persisting each as a governance-log event and a progress update — so the trail is
the real transfer's, not a fabricated one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """One progress tick from a download: ``percent`` in ``0..100`` and an
    optional human-readable ``note`` (e.g. ``"fetching shard 2/4"``)."""

    percent: int
    note: str | None = None


@runtime_checkable
class Downloader(Protocol):
    """A pluggable byte-fetcher for a local model.

    ``fetch`` yields :class:`DownloadProgress` values as the transfer advances and
    returns normally on success; it raises to signal a failed download (the
    service maps that onto the model's ``error`` state). Implementations must be
    generators so the caller can persist each tick before requesting the next."""

    def fetch(self, *, model_id: str, provenance: str | None) -> Iterator[DownloadProgress]:
        ...


class StubDownloader:
    """A deterministic, network-free downloader for tests and offline runs.

    Emits ``steps`` evenly spaced progress ticks ending at 100. ``fail_at``, if
    set, raises :class:`DownloadFailed` once that percent is reached, so a test
    can exercise the failure path without any real I/O."""

    def __init__(self, *, steps: int = 4, fail_at: int | None = None) -> None:
        if steps < 1:
            raise ValueError("steps must be >= 1")
        self.steps = steps
        self.fail_at = fail_at

    def fetch(
        self, *, model_id: str, provenance: str | None
    ) -> Iterator[DownloadProgress]:
        for step in range(1, self.steps + 1):
            percent = round(step * 100 / self.steps)
            if self.fail_at is not None and percent >= self.fail_at:
                raise DownloadFailed(
                    f"stub download of {model_id!r} failed at {percent}%"
                )
            yield DownloadProgress(percent=percent, note=f"step {step}/{self.steps}")


class DownloadFailed(Exception):
    """Raised by a :class:`Downloader` when a transfer cannot complete."""
