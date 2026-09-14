"""Seed data for the decision-memory graph (VOYN-MIN-GRAPH-SQL).

Every incident below is real: mined from migration comments, commit messages
and module docstrings already in this repository, not invented for the demo.
Each becomes one decision -> dependency -> error -> effect(failure) chain,
plus the corrective decision that mitigated it. Sources are named in each
node's `incident_ref` so a reader can go verify the claim.
"""

from __future__ import annotations

from command_center import decision_graph as graph_module
from command_center.decision_graph import DecisionGraph


def _chain(
    graph: DecisionGraph,
    incident_ref: str,
    *,
    decision: tuple[str, str],
    dependency: tuple[str, str],
    error: tuple[str, str],
    effect: tuple[str, str],
    fix: tuple[str, str],
) -> None:
    d = graph_module.add_node(graph, node_type="decision", title=decision[0], detail=decision[1],
                               incident_ref=incident_ref)
    dep = graph_module.add_node(graph, node_type="dependency", title=dependency[0],
                                 detail=dependency[1], incident_ref=incident_ref)
    e = graph_module.add_node(graph, node_type="error", title=error[0], detail=error[1],
                               incident_ref=incident_ref)
    eff = graph_module.add_node(graph, node_type="effect", title=effect[0], detail=effect[1],
                                 incident_ref=incident_ref, is_failure=True)
    f = graph_module.add_node(graph, node_type="decision", title=fix[0], detail=fix[1],
                               incident_ref=incident_ref)

    graph_module.add_edge(graph, from_id=d["id"], to_id=dep["id"], relation="leads_to")
    graph_module.add_edge(graph, from_id=dep["id"], to_id=e["id"], relation="causes")
    graph_module.add_edge(graph, from_id=e["id"], to_id=eff["id"], relation="causes")
    graph_module.add_edge(graph, from_id=f["id"], to_id=eff["id"], relation="mitigates")


INCIDENTS = [
    dict(
        incident_ref="VOYN-W0-AICC-DEFER-AUTO-RESUME-REM (migrations 0014, 0017)",
        decision=(
            "Bound auto-resume at 3 GRANTED resumes, counted over a task's whole life",
            "backlog_resume_deferred() (migration 0014) capped how many times a "
            "technically-parked task could be auto-resumed, to stop an unbounded retry "
            "loop from masking a real outage.",
        ),
        dependency=(
            "Dead-codex-era outages spanning weeks",
            "A period of recurring pipeline outages meant some tasks re-armed the same "
            "technical park many times over a long span, not in one burst.",
        ),
        error=(
            "Lifetime count conflates a burst with unlimited chances",
            "Counting resumes over the task's whole life cannot distinguish three "
            "resumes in one bad hour from three resumes spread across weeks of "
            "outliving repeated transient failures.",
        ),
        effect=(
            "Tasks stuck DEFER_TO_USER forever after the pipeline was fixed",
            "Once the lifetime budget was exhausted, the fixed pipeline could no "
            "longer reclaim the park -- unreachable by the very automation built to "
            "resume it, recoverable only by manual triage.",
        ),
        fix=(
            "Replace the lifetime budget with a 48-hour sliding window (migration 0017)",
            "Wide enough that a nightly recurring failure still trips it; narrow "
            "enough that a fixed pipeline reclaims its parks within two days with no "
            "human in the loop.",
        ),
    ),
    dict(
        incident_ref="VOYN-W0-AICC-TASK-IMPORT-CONCURRENCY-FLAKE (#507, #727)",
        decision=(
            "load_tasks() seeds an empty store when tasks.json is missing",
            "tasks_repository.load_tasks called save_tasks(root, []) unconditionally "
            "on the read path whenever the file did not yet exist.",
        ),
        dependency=(
            "The seed-on-read runs outside the store's flock",
            "JSONTasksRepository.load_all() is called before task_import."
            "apply_task_package takes the per-write lock, so the existence check and "
            "the seed write are not covered by the same critical section as the "
            "importer's own writes.",
        ),
        error=(
            "Two importers both observe a missing tasks.json",
            "The faster importer commits its first task under the lock; the slower "
            "importer's empty seed, decided before either lock was taken, then lands "
            "on top and erases it.",
        ),
        effect=(
            "The first task id of a bulk import package silently disappears",
            "Reproduced locally under CPU oversubscription: 4 failures out of 60 "
            "runs, every writer reporting success and no exception swallowed -- a "
            "real lost record, not a flaky assertion.",
        ),
        fix=(
            "Make the seed an exclusive create (create_json_if_absent, via os.link)",
            "A check-then-write race cannot be closed by a lock when the racing party "
            "is a read; publishing by os.link fails rather than replaces when the "
            "target already exists, so the loser's empty seed can never overwrite "
            "the winner's committed content.",
        ),
    ),
    dict(
        incident_ref="VOYN-W0-AICC-MIGRATOR-PASSWORD-FLAKE",
        decision=(
            "Give every product role a session-scoped password fixture, one per xdist worker",
            "role_passwords is scoped 'session' meaning per xdist WORKER PROCESS, "
            "each independently creating and password-setting the same cluster-wide "
            "roles for its own tests.",
        ),
        dependency=(
            "Roles are cluster-level objects shared by every worker",
            "aicc_migrator and its siblings exist once per PostgreSQL cluster, not "
            "once per test database, so N worker processes contend for the same "
            "catalog rows.",
        ),
        error=(
            "Check-then-create races across independent transactions",
            "'does the role exist' and 'create it' are two statements; an advisory "
            "lock held only inside one worker's own transaction does not serialize "
            "it against a different worker's concurrent transaction doing the same "
            "check.",
        ),
        effect=(
            "\"password authentication failed for user aicc_migrator\" in CI",
            "Live 2026-08-21: one worker's ALTER ROLE ... PASSWORD overwrote the "
            "cluster-wide role's password with its own value after another worker "
            "had already authenticated as that role, failing every connection after.",
        ),
        fix=(
            "pg_advisory_xact_lock inside the same DO block as the check and the create",
            "Cluster-scoped like the role it guards, released automatically when the "
            "block's transaction ends -- protects every caller by construction "
            "instead of depending on each call site remembering to wrap itself.",
        ),
    ),
    dict(
        incident_ref="VOYN-W0-AICC-RUNS-READ-ZERO (#733)",
        decision=(
            "build_projection() degrades to empty lanes/events on any read failure",
            "A broad except around list_unified_runs() meant a genuinely empty "
            "runtime.db and a database that errors on read were both handled the "
            "same way: empty output, no error surfaced.",
        ),
        dependency=(
            "Several unrelated conditions can raise inside that one read",
            "Schema drift, a migration mismatch, or a bad path can each raise from "
            "list_unified_runs(), and the catch could not tell them apart from a "
            "database that is simply empty.",
        ),
        error=(
            "The caught exception was discarded, not logged",
            "Nothing recorded which of those conditions actually happened, so the "
            "artifact written out looked identical either way.",
        ),
        effect=(
            "\"Returns 0, no idea why\" on the next live run",
            "An empty runtime.db and an erroring read were indistinguishable from "
            "the written artifact -- the exact failure mode this fix was written "
            "against.",
        ),
        fix=(
            "Log the traceback behind a degraded run journal",
            "Turns a silent empty result into an actionable signal the next time "
            "the same read fails against the real database.",
        ),
    ),
    dict(
        incident_ref="VOYN-W0-AICC-DISPATCH-PLAN-FABRICATED-SPEND-REM (#641 rejected, #706 fix)",
        decision=(
            "PR #641 redacts only the reported daily_spend_usd under the kill-switch path",
            "When live spend measurement was unavailable, the kill-switch path "
            "hid daily_spend_usd but left the plan's other two spend fields alone.",
        ),
        dependency=(
            "projected_spend_usd and budget_remaining_usd are derived from the same plan",
            "Both fields were computed from a fabricated ceiling value that stood in "
            "for the unmeasured spend, independently of whichever field got redacted.",
        ),
        error=(
            "The fabricated ceiling still leaked into projected/remaining",
            "Tests asserted that leak as correct, so a plan with unmeasured spend "
            "shipped projected_spend_usd/budget_remaining_usd as if they were real "
            "measurements.",
        ),
        effect=(
            "A fabricated budget ceiling ships as if it were a measured value",
            "An operator reading the dispatch plan could not tell an unmeasured "
            "kill-switch estimate from a real spend measurement in two of its "
            "three numbers.",
        ),
        fix=(
            "Add DispatchPlan.spend_measurement (status/kind) as a required field",
            "projected_spend_usd is now derived from the same None daily_spend_usd "
            "used by the kill-switch/unknown-budget path in all three fields, so a "
            "plan built outside plan_dispatch can never silently inherit a "
            "\"measured\" claim.",
        ),
    ),
    dict(
        incident_ref="VOYN-W0-AICC-GITLEAKS-TEST-FIXTURE-FINGERPRINT (#766)",
        decision=(
            "Run the secret scan gate with fetch-depth: 0",
            "Scanning full history, not just the diff, so a secret committed on any "
            "branch is caught regardless of which PR's diff is being checked.",
        ),
        dependency=(
            "A synthetic test password fixture lives in tests/db/test_pool_rotation.py",
            "The psycopg DSN-redaction branch carries a deliberately fake password "
            "used only to exercise the redaction path.",
        ),
        error=(
            "The scanner cannot distinguish a synthetic fixture from a real secret",
            "With full history visible, that one fixture line was flagged on every "
            "open PR, not just the branch that introduced it.",
        ),
        effect=(
            "The Secret scan gate fails on every open PR",
            "Reproduced on PR #762: an unrelated change blocked on a merge gate for "
            "a password that was never real and never left the test fixture.",
        ),
        fix=(
            "Allowlist by exact commit:file:rule:line fingerprint",
            "One specific fingerprint, nothing broader -- a real secret anywhere "
            "else, including a different line of the same file, still fails the "
            "gate.",
        ),
    ),
]


def build_graph() -> DecisionGraph:
    """A fresh `DecisionGraph` seeded with every incident in `INCIDENTS`."""
    graph = graph_module.new_graph()
    for incident in INCIDENTS:
        _chain(graph, **incident)
    return graph
