"""The store contract the execution queue is mirrored into.

The queue already has two stores: `execution_queue.json` is authoritative and
`runtime.db` carries a mirror written alongside it (ADR 0007). Moving the
runtime onto PostgreSQL (VOYN-W0-AICC-SRV-01B) adds a third, and the migration
gate — "stop writing JSON once a session shows no divergence" — has to hold
against *every* mirror, not against whichever one the check happens to know
about.

So the mirror becomes an interface rather than a hardcoded destination. Two
consequences are the point of doing it this way:

* `queue_divergence` can compare the authority against any mirror without
  knowing which store it is talking to, so adding PostgreSQL cannot silently
  narrow the check that gates the cutover;
* a mirror that is unreachable is reported, never treated as agreement. An
  absent store has nothing to disagree with, and a gate satisfied by absence is
  a gate that advances the migration on the strength of a store nobody wrote.

The entry shape is deliberately the JSON entry itself — a plain dict — so a
divergence check compares like with like and has no translation step of its own
to be wrong about.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class QueueMirror(Protocol):
    """A store the execution queue is mirrored into.

    Implementations must be *idempotent per replace*: `replace_entries` sets the
    queue to exactly the given list rather than appending to it. The backfill is
    expected to run more than once — once per operator, once after a rollback
    and re-advance — and an appending mirror would manufacture exactly the
    divergence the backfill exists to remove.
    """

    #: Stable identifier used in divergence records, so an operator can tell
    #: *which* mirror disagreed without reading the code.
    name: str

    def replace_entries(self, entries: list[dict]) -> None:
        """Set the mirror's queue to `entries`, in this order."""

    def list_entries(self) -> list[dict]:
        """Every entry in stored order, shaped like a JSON queue entry."""
