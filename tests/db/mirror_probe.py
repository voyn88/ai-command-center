"""The perturbation independent review had to build by hand, twice.

Slices 4 and 5 both shipped an end-to-end reconciliation whose docstring
claimed more coverage than it had, and both times the disproof was the same
experiment: run the scenario repeatedly, fail exactly one mirror write per run,
and see whether anything notices. Slice 5's second rejection came from doing it
by ordinal — a probe that stopped reaching the defect the moment the scenario
changed — which is why this helper counts the writes from the *current* code on
every run instead of taking a number.

Using it in a test turns "I believe every lost write is caught" into a
statement the suite proves per write, with the failing stage named.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class LostWrite:
    """One run of the scenario with a single mirror write made to fail."""

    #: 0-based index of the write that was failed, in call order.
    index: int
    #: The mirror attribute whose `upsert` raised, e.g. `PostgresContactMirror`.
    target: str
    #: Whether the check noticed. `False` on any row means a lost write is
    #: invisible to whatever the test passed as `noticed`.
    noticed: bool


class _Failing:
    def __init__(self, real: object, counter: dict, fail_at: int, target: str) -> None:
        self._real = real
        self._counter = counter
        self._fail_at = fail_at
        self._target = target

    def _tick(self) -> bool:
        index = self._counter["n"]
        self._counter["n"] += 1
        self._counter["calls"].append(self._target)
        return index == self._fail_at

    def upsert(self, record: dict) -> None:
        if self._tick():
            raise RuntimeError(f"injected failure on mirror write #{self._fail_at}")
        self._real.upsert(record)

    def __getattr__(self, name: str) -> object:  # delete_day, resync_identity, ...
        return getattr(self._real, name)


def each_lost_write_is_noticed(
    monkeypatch,
    targets: Iterable[tuple[object, Sequence[str], Callable[[], object]]],
    scenario: Callable[[], None],
    noticed: Callable[[], bool],
) -> list[LostWrite]:
    """Fail one mirror write per run; report whether `noticed()` saw each one.

    `targets` is `(store_module, attribute_names, build_real_mirror)`: the
    module whose mirror classes the dual-write hooks import, the attribute names
    to patch, and a factory for the working mirror the patched one delegates to.

    The write count is measured by a first, unperturbed run, so a scenario that
    grows a write later cannot leave this probe testing a stale position — the
    mistake slice 5 made and its acceptance caught.

    `noticed()` must observe the *mirror*, not the authority: a reconciliation,
    not a row count. It is called after the scenario, with the failure still in
    place.
    """
    counter = _install(monkeypatch, targets, fail_at=-1)
    scenario()
    order = list(counter["calls"])
    total = len(order)
    if total == 0:
        raise AssertionError("the scenario performed no mirror writes")

    results: list[LostWrite] = []
    for index in range(total):
        counter = _install(monkeypatch, targets, fail_at=index)
        scenario()
        results.append(LostWrite(index=index, target=order[index], noticed=bool(noticed())))
    return results


def _install(monkeypatch, targets, *, fail_at: int) -> dict:
    counter: dict = {"n": 0, "calls": []}
    for module, names, build in targets:
        for name in names:
            monkeypatch.setattr(
                module,
                name,
                lambda build=build, name=name: _Failing(build(), counter, fail_at, name),
            )
    return counter
