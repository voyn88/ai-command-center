"""VOYN-W0-AICC-ARCHITECTURE-FITNESS-GAPS fitness gate: all new event
consumers are idempotent.

``command_center.events.EventBus`` makes no redelivery guarantee of its own
(:mod:`command_center.events.bus`) — a subscriber may see the same logical
event more than once (a replayed digest run, a re-imported fixture, a retried
publish). The three consumers wired onto it today (``OwnerAutofill``,
``CouncilIntake``, ``ConflictIntake``) each document and implement the same
convention to make that safe: every event-raised row carries a stable
``source_ref`` (``"promotion:<id>"``, ``"proposal:<id>"``, ``"incident:<id>"``)
and the handler skips creation when a row with that ref already exists — see
the "Idempotency" section of each module's docstring
(``command_center/digest/owner_autofill.py``,
``command_center/council/intake.py``, ``command_center/conflicts/intake.py``).

Per-consumer behavioral tests already assert this for each of the three
(``tests/test_owner_autofill.py::test_event_to_item_is_idempotent_on_replay``,
``tests/test_council_intake.py::test_intake_dedups_by_source_ref``,
``tests/test_conflict_intake.py::test_incident_intake_dedups_by_source_ref``).
What none of them catch is a *new* consumer added later that forgets the
convention — each of those tests only exercises the consumer it already knows
about. This gate instead discovers every ``<bus>.subscribe(EventType,
handler)`` call under ``command_center/`` and requires the handler's own body
to reference ``source_ref`` — a structural proxy for "keys off a stable
per-event dedup ref" cheap enough to run as a static check, so a consumer
that skips the convention fails CI on the strength of its own source, not
because someone remembered to write a redelivery test for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMAND_CENTER = REPO_ROOT / "command_center"

DEDUP_MARKER = "source_ref"


def _python_files(root: Path) -> list[Path]:
    files = sorted(root.rglob("*.py"))
    assert files, f"no python files under {root} — perimeter moved?"
    return files


def _subscribe_handler_names(tree: ast.AST) -> list[str]:
    """Callable names passed as the handler to every ``*.subscribe(Event, handler)``."""
    handlers: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "subscribe"
            and len(node.args) >= 2
        ):
            continue
        handler = node.args[1]
        if isinstance(handler, ast.Attribute):
            handlers.append(handler.attr)
        elif isinstance(handler, ast.Name):
            handlers.append(handler.id)
    return handlers


def _function_source(source: str, tree: ast.AST, name: str) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return None


def _discover_consumers() -> dict[str, list[str]]:
    """``{"command_center/foo.py": ["on_bar", ...]}`` for every subscribed handler
    that is defined (not merely referenced) in that file."""
    consumers: dict[str, list[str]] = {}
    for path in _python_files(COMMAND_CENTER):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = []
        for handler_name in _subscribe_handler_names(tree):
            if _function_source(source, tree, handler_name) is not None:
                names.append(handler_name)
        if names:
            consumers[str(path.relative_to(REPO_ROOT))] = names
    return consumers


def test_discovery_finds_the_known_event_consumers():
    """Guards the guard: if AST discovery regresses to finding nothing, the
    dedup check below would pass vacuously. Pin the three known consumers so
    that regression is visible here first."""
    consumers = _discover_consumers()
    assert consumers.get("command_center/digest/owner_autofill.py")
    assert consumers.get("command_center/council/intake.py")
    assert consumers.get("command_center/conflicts/intake.py")


def test_every_event_consumer_handler_dedups_by_source_ref():
    offenders: dict[str, list[str]] = {}
    for path in _python_files(COMMAND_CENTER):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for handler_name in _subscribe_handler_names(tree):
            body = _function_source(source, tree, handler_name)
            if body is None:
                continue
            if DEDUP_MARKER not in body:
                offenders.setdefault(str(path.relative_to(REPO_ROOT)), []).append(
                    handler_name
                )
    assert not offenders, (
        "event-bus subscriber handler(s) with no source_ref dedup guard — a "
        f"redelivered event can double-create: {offenders}"
    )
