"""VOYN-W0-AICC-SRV-07h: every non-SQLite authority gets an SRV-07 verdict.

SRV-07's own formulation covers "SQLite/JSON/JSONL," but the machinery that
grew to serve it (`tests/db/test_mirror_coverage.py`'s Gate A/B) only ever
asked about SQLite-authority tables. `queue_entry` was the one JSON-authority
case that got a contract of its own (`command_center/db/queue_store.py`) and,
until this module, that was also the *only* one anybody had checked — every
other JSON/JSONL store in `docs/AUTHORITY_MAP.md` had no recorded position on
whether it is in SRV-07's scope at all.

`docs/AUTHORITY_MAP.md` is already the enforced, exhaustive inventory of every
store under `data/` (`tests/architecture/test_authority_map.py` fails when a
new one ships undocumented). This module does not re-discover stores; it reads
that map's JSON and JSONL sections and requires each store to carry a signed
verdict: `excluded` (out of SRV-07's scope, with a reason) or `owned` (tracked
by a specific task — which may already be done, like `queue_entry`, or may be
new work). A store with neither is exactly the class of gap SRV-07h exists to
close: present, live, and silently uncounted.

No database is needed here, deliberately — this is a documentation-consistency
gate, and it must keep running on a machine with no PostgreSQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_MAP = REPO_ROOT / "docs" / "AUTHORITY_MAP.md"

_JSON_HEADING = "## JSON (file-locked, atomic-replace via `command_center/storage.py`)"
_JSONL_HEADING = "## JSONL (append-only, crash-truncatable)"
_OTHER_HEADING = "## Other"
_STORE_RE = re.compile(r"`(data/[\w.-]+\.jsonl?)`")


def _authorities_in(text: str) -> set[str]:
    """Every `data/*.json(l)` path named between the JSON and Other headings.

    Bounded by `## Other` rather than reading to end-of-file so that a future
    section appended after `## Other` (there already is one, "Deployed truth")
    cannot smuggle an unrelated backtick-quoted path into the count. Asserting
    the JSONL heading falls inside that span is the positive control: if a doc
    edit ever moved JSONL content out of it, this would start silently seeing
    an empty JSONL section instead of failing.
    """
    start = text.index(_JSON_HEADING)
    end = text.index(_OTHER_HEADING, start)
    section = text[start:end]
    assert _JSONL_HEADING in section, (
        "the JSONL section moved outside the JSON..Other span this scan reads"
    )
    return set(_STORE_RE.findall(section))


def _documented_authorities() -> set[str]:
    return _authorities_in(AUTHORITY_MAP.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Disposition:
    """A signed verdict on whether a store is SRV-07's problem.

    `scope` is `"excluded"` (SRV-07 does not touch this store, and `reason`
    says why) or `"owned"` (a task is responsible for it — `task` may already
    be delivered, as with `queue_entry`, or may be new work this inventory is
    the one to open). Both fields are required for the same reason
    `tests/db/test_mirror_coverage.py`'s `Exclusion` requires them: a reason
    without an owning task is an opinion nobody will revisit, and a task
    without a reason sends the next reader to the backlog to find out what was
    decided here.
    """

    scope: str
    reason: str
    task: str


# ---------------------------------------------------------------------------
# The inventory — VOYN-W0-AICC-SRV-07h
# ---------------------------------------------------------------------------
#
# SRV-07's importer target is the accepted 33-table PostgreSQL schema
# (`docs/postgres-foundation.md`, `docs/srv01b-schema-map.md`): it converts and
# loads rows against tables that already exist. A JSON/JSONL store with no
# matching table in that schema has nothing for an *importer* to write into —
# moving it to PostgreSQL first needs a schema-design decision, which is a
# different kind of task than SRV-07's. That split, not "is this data
# important," is what separates `excluded` from `owned` below: importance
# decides whether the new schema-design task is worth opening, not whether
# SRV-07 itself claims the store.

JSON_AND_JSONL_AUTHORITY_DISPOSITIONS: dict[str, Disposition] = {
    "data/execution_queue.json": Disposition(
        scope="owned",
        reason=(
            "The trigger case for this whole inventory: the JSON file stays "
            "authoritative by design (whole-list replacement, a `position` "
            "column the mirror's own contract needed and the shared "
            "`PostgresTableMirror` contract does not model), mirrored into "
            "`queue_entry` through `command_center/db/queue_store.py` rather "
            "than converted-and-loaded like the 33-table schema's SQLite side. "
            "Already delivered, not new work — recorded so the store has a "
            "verdict instead of being visible only through `queue_entry`'s own "
            "exclusion entry."
        ),
        task="VOYN-W0-AICC-QUEUE-ENTRY-PARITY",
    ),
    "data/runs.jsonl": Disposition(
        scope="excluded",
        reason=(
            "Documented as frozen in `docs/AUTHORITY_MAP.md`: nothing writes it "
            "anymore, and it is a one-way, non-destructive, already-idempotent "
            "read source for `runtime/legacy_import.py`, which lands its rows in "
            "the `run` table — a table the shared mirror contract already "
            "covers. There is no live JSON authority left here for a separate "
            "SRV-07 import to target; importing `run` already carries this "
            "file's history forward."
        ),
        task="VOYN-W0-AICC-SRV-07h",
    ),
    "data/tasks.json": Disposition(
        scope="owned",
        reason=(
            "The Kanban/product task board — titles, lanes, deps, workflow "
            "fields — is fleet-visible product data with active concurrent "
            "writers (`tasks_repository.mutate_tasks`), not operator "
            "configuration. No table in the accepted 33-table schema targets "
            "it, so an SRV-07 importer has nowhere to write these rows; moving "
            "it to PostgreSQL needs a schema-design decision first, which "
            "SRV-07's own charter (import against an accepted schema) does not "
            "cover."
        ),
        task="VOYN-W0-AICC-TASKBOARD-PG-MIGRATION",
    ),
    "data/portfolio_launches.json": Disposition(
        scope="owned",
        reason=(
            "Portfolio launch records (branch/worktree provisioning outcomes "
            "per card) are fleet-relevant history with active writers "
            "(`portfolio_launch.py`) and no matching table in the accepted "
            "schema, the same gap as `tasks.json`: a schema-design task is "
            "needed before any importer has somewhere to put these rows."
        ),
        task="VOYN-W0-AICC-PORTFOLIO-LAUNCH-PG-MIGRATION",
    ),
    "data/activity.jsonl": Disposition(
        scope="owned",
        reason=(
            "The operator-visible activity feed is fleet-relevant, audit-"
            "adjacent history (task/run/conversation events), not local "
            "configuration, and has no matching table in the accepted schema. "
            "As with `tasks.json`, the missing piece is a table to import into, "
            "which is schema-design work outside SRV-07's own charter."
        ),
        task="VOYN-W0-AICC-ACTIVITY-LOG-PG-MIGRATION",
    ),
    "data/chats.json": Disposition(
        scope="excluded",
        reason=(
            "`docs/AUTHORITY_MAP.md` already states the verdict this entry "
            "just has to agree with: 'Non-critical; loss is acceptable by "
            "design.' That removes the only motivation (durability, cross-"
            "instance consistency) that justifies the engineering cost of a "
            "schema-design-plus-import task for the other product stores above; "
            "there is nothing here a migration would protect that the current "
            "design does not already accept losing."
        ),
        task="VOYN-W0-AICC-SRV-07h",
    ),
    "data/pipeline_settings.json": Disposition(
        scope="excluded",
        reason=(
            "Single-machine operator configuration by explicit design (autopilot "
            "opt-ins, concurrency caps, spend limits) with fail-closed parsing "
            "built around being a local file — its own docstring cites ADR 0003 "
            "reserving `runtime.db`, and by the same reasoning the accepted "
            "PostgreSQL schema, for execution state rather than config. No table "
            "targets it and none is proposed; centralising autopilot config "
            "across replicas is a product/ops decision, not a data import."
        ),
        task="VOYN-W0-AICC-SRV-07h",
    ),
    "data/dispatch_policy.json": Disposition(
        scope="excluded",
        reason=(
            "As `pipeline_settings.json`: its own docstring states 'Policy "
            "only — never execution truth,' mirrors that module's atomic-"
            "write-plus-lock convention on purpose, and has no matching table "
            "in the accepted schema. The same ADR 0003 boundary applies."
        ),
        task="VOYN-W0-AICC-SRV-07h",
    ),
    "data/project_config.json": Disposition(
        scope="excluded",
        reason=(
            "Holds this machine's own repository filesystem paths. A shared "
            "PostgreSQL row for 'where project X lives on disk' is meaningless "
            "without a host key the current design has no reason to add, since "
            "the path is never valid on any other machine; the store is "
            "host-local by construction, not merely undelivered to Postgres."
        ),
        task="VOYN-W0-AICC-SRV-07h",
    ),
    "data/portfolio_config.json": Disposition(
        scope="excluded",
        reason=(
            "As `project_config.json`, and by its own docstring's admission: "
            "'mirroring `project_config.py`'s own `project_config.json` "
            "convention exactly.' Same host-local repository paths, same "
            "absence of cross-machine meaning."
        ),
        task="VOYN-W0-AICC-SRV-07h",
    ),
    "data/integration_registry.json": Disposition(
        scope="excluded",
        reason=(
            "Its own docstring: 'Operator configuration, never execution "
            "truth' — locally-configured repository paths and `gh` remotes, "
            "gitignored, never committed. The same host-local-path reasoning "
            "as `project_config.json` applies: a shared store would need a "
            "host key the design has no use for."
        ),
        task="VOYN-W0-AICC-SRV-07h",
    ),
}


# ---------------------------------------------------------------------------
# The gate, as functions, so a test can feed it a store that is neither
# ---------------------------------------------------------------------------


def _undeclared(authorities: set[str], declared: dict[str, Disposition]) -> list[str]:
    return sorted(authorities - set(declared))


def _stale(authorities: set[str], declared: dict[str, Disposition]) -> list[str]:
    return sorted(name for name in declared if name not in authorities)


def test_every_documented_json_or_jsonl_authority_has_a_disposition() -> None:
    undeclared = _undeclared(
        _documented_authorities(), JSON_AND_JSONL_AUTHORITY_DISPOSITIONS
    )
    assert undeclared == [], (
        "JSON/JSONL authorities in docs/AUTHORITY_MAP.md with no SRV-07 verdict: "
        f"{undeclared}. Add an entry to JSON_AND_JSONL_AUTHORITY_DISPOSITIONS "
        "with scope='excluded' (+ reason) or scope='owned' (+ owning task)."
    )


def test_no_disposition_outlives_its_store() -> None:
    stale = _stale(_documented_authorities(), JSON_AND_JSONL_AUTHORITY_DISPOSITIONS)
    assert stale == [], f"dispositions for stores no longer in the map: {stale}"


def test_the_coverage_gate_fails_on_a_store_the_map_gained() -> None:
    """The gate, shown to bite — same shape as `test_mirror_coverage.py`'s.

    A coverage rule that has only ever been observed passing is
    indistinguishable from one that always returns the empty set.
    """
    synthetic = (
        f"{_JSON_HEADING}\n\n"
        "| Store | Authority | Writer | Recovery |\n"
        "|---|---|---|---|\n"
        "| `data/a_store_nobody_declared.json` | test double | test.py | n/a |\n\n"
        f"{_JSONL_HEADING}\n\n"
        "| Store | Authority | Writer | Recovery |\n"
        "|---|---|---|---|\n"
        f"{_OTHER_HEADING}\n\n"
    )
    found = _authorities_in(synthetic)
    assert found == {"data/a_store_nobody_declared.json"}
    assert _undeclared(found, JSON_AND_JSONL_AUTHORITY_DISPOSITIONS) == [
        "data/a_store_nobody_declared.json"
    ]

    signed = dict(
        JSON_AND_JSONL_AUTHORITY_DISPOSITIONS,
        **{
            "data/a_store_nobody_declared.json": Disposition(
                scope="excluded", reason="test double", task="VOYN-TEST"
            )
        },
    )
    assert _undeclared(found, signed) == []


def test_the_json_and_jsonl_headings_are_found_inside_the_json_to_other_span() -> None:
    """The positive control for `_authorities_in`'s own assumption.

    If a doc edit ever moved the JSONL section outside the span this scan
    reads, `_authorities_in` would silently start reporting fewer stores
    instead of failing — this pins the assumption directly against the real
    file, not just the synthetic fixture above.
    """
    text = AUTHORITY_MAP.read_text(encoding="utf-8")
    start = text.index(_JSON_HEADING)
    end = text.index(_OTHER_HEADING, start)
    assert start < text.index(_JSONL_HEADING) < end


def test_every_disposition_is_signed() -> None:
    """As `test_mirror_coverage.py`'s `test_every_exclusion_is_signed`.

    The failure this prevents is a disposition added under deadline with an
    empty reason, which reads as a decision in the diff and is an omission in
    fact.
    """
    assert JSON_AND_JSONL_AUTHORITY_DISPOSITIONS, (
        "an empty registry would satisfy every check above"
    )
    for store, disposition in JSON_AND_JSONL_AUTHORITY_DISPOSITIONS.items():
        assert disposition.scope in {"excluded", "owned"}, (
            f"{store}: {disposition.scope!r} is not a recognised scope"
        )
        assert len(disposition.reason.split()) >= 10, (
            f"{store}: the reason is not a reason"
        )
        assert re.fullmatch(r"VOYN-[A-Z0-9-]+[a-z]?", disposition.task), (
            f"{store}: {disposition.task!r} is not a central backlog task id"
        )


@pytest.mark.parametrize(
    "store",
    sorted(
        store
        for store, disposition in JSON_AND_JSONL_AUTHORITY_DISPOSITIONS.items()
        if disposition.scope == "owned"
    ),
)
def test_owned_dispositions_name_a_task_distinct_from_this_inventory_task(
    store: str,
) -> None:
    """`owned` means *someone* is responsible — not that filing this entry is enough.

    A disposition that names `VOYN-W0-AICC-SRV-07h` itself as the owning task
    would let "needs its own task" be satisfied by the inventory noting that it
    needs one, without anything actually tracking the work. `excluded` entries
    may cite this task, because recording the exclusion *is* the whole of the
    work; `owned` entries may not.
    """
    task = JSON_AND_JSONL_AUTHORITY_DISPOSITIONS[store].task
    assert task != "VOYN-W0-AICC-SRV-07h", (
        f"{store}: an 'owned' disposition needs a real owning task, not this inventory task"
    )
