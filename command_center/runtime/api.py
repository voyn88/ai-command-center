"""Minimal Execution Center backend API.

Exactly the service surface Sprint 1 calls for: list sessions, list runs,
inspect run status, read incremental events, request cancellation, reconcile
stale runs (plus launching a run, since nothing else in this codebase does
that yet for v2). This is a plain Python facade over `supervisor.Supervisor`
and `db.py` — no HTTP layer, no redesigned Streamlit UI.
`scripts/execution_center_debug.py` is the minimal debug view used to
validate this module end to end, per the Sprint 1 brief.

`start_run` is the **only** application-facing launch route, and it is
deliberately *not* a thin passthrough to a raw prompt: it takes a user
`instruction` plus optional `candidate_content`/`confirmed_items`, always
calls `context_service.assemble_context` itself, and only then builds the
final provider prompt (`context_service.build_prompt`). A caller cannot
bypass the BANK/LEGAL sensitive-content boundary by pre-building a prompt
and handing it in — there is no parameter here that accepts one. The
low-level `supervisor.Supervisor.start_raw` (which does accept a raw,
already-final prompt) is intentionally not exposed as `ExecutionCenterAPI`
launch method; it exists for `start_run` itself to call and for tests that
need to exercise process-lifecycle mechanics directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, TextIO

from command_center import project_config, provider_route, workspace_provisioning
from command_center import run_lineage as provenance
from command_center.runtime import autonomy, autonomy_service, context_service, db, scheduler, supervisor

DEFAULT_TIMEOUT_SECONDS = 900


class ExecutionCenterAPI:
    def __init__(
        self,
        db_path: Path | None = None,
        sup: supervisor.Supervisor | None = None,
        *,
        maintenance_token: str | None = None,
        maintenance_lock_handle: TextIO | None = None,
        enable_completion_autopilot: bool | None = None,
    ) -> None:
        self.supervisor = sup or supervisor.Supervisor(
            db_path,
            maintenance_token=maintenance_token,
            maintenance_lock_handle=maintenance_lock_handle,
            enable_completion_autopilot=enable_completion_autopilot,
        )
        self.db_path = self.supervisor.db_path

    # ------------------------------------------------------------------
    # Launch (requires explicit confirmation; enforced in Supervisor.start_raw)
    # ------------------------------------------------------------------

    def start_run(
        self,
        *,
        project: str,
        repository_path: str,
        task_type: str,
        instruction: str,
        confirmed: bool,
        candidate_content: dict[str, str] | None = None,
        confirmed_items: list[str] | None = None,
        metadata: dict | None = None,
        task_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
        is_resume: bool = False,
        model: str | None = None,
        timeout_seconds: int | None = DEFAULT_TIMEOUT_SECONDS,
        expected_branch: str | None = None,
        launch_source: str | None = None,
        prompt_version: int | None = None,
        capability_override: str | None = None,
        repository_already_validated: bool = False,
        workspace_verification: workspace_provisioning.WorkspaceSpec | None = None,
        executor_id: str = "claude_code",
        provider_route_ids: tuple[str, ...] | None = None,
        max_provider_attempts: int | None = None,
        max_global_concurrency: int | None = None,
        untrusted: bool = False,
        operator_elevated: bool = False,
    ) -> dict:
        """Launch a run. The final prompt sent to `claude` is always built
        internally from `instruction` plus whatever `context_service.
        assemble_context` decides to include — for a sensitive project
        (BANK/LEGAL, decided by `project_config`, never overridable by the
        caller), any `candidate_content` item is included only if its key is
        also in `confirmed_items`. The resulting outbound-content manifest
        (which content keys actually left the machine, and which were
        excluded) is persisted as a `context_manifest` run event — auditable
        via `get_events` — and also returned under `run["context_manifest"]`.

        `timeout_seconds` defaults to 900s; pass `None` explicitly to disable
        the automatic timeout for this run.
        """
        allowed_providers = tuple(project_config.allowed_execution_providers(project))
        project_config.require_execution_provider_allowed(project, executor_id)
        for candidate in provider_route_ids or ():
            project_config.require_execution_provider_allowed(project, candidate)
        route = provider_route.ProviderRoute.from_policy(
            allowed_providers=allowed_providers,
            preferred_provider=executor_id,
            explicit_providers=provider_route_ids,
            policy_version="project_allowed_agents_v1",
        )
        if max_provider_attempts is not None:
            route = provider_route.ProviderRoute(
                route.providers,
                max_attempts=max_provider_attempts,
                selection_reason=route.selection_reason,
                policy_version=route.policy_version,
            )
        context = context_service.assemble_context(
            project_id=project,
            metadata=metadata,
            candidate_content=candidate_content,
            confirmed_items=confirmed_items,
        )
        prompt = context_service.build_prompt(instruction, context)
        manifest = context_service.build_outbound_manifest(context)

        run = self.supervisor.start_raw(
            project=project,
            repository_path=repository_path,
            task_type=task_type,
            prompt=prompt,
            confirmed=confirmed,
            task_id=task_id,
            title=title or ("Codex CLI run" if executor_id == "codex" else instruction[:120]),
            session_id=session_id,
            is_resume=is_resume,
            model=model,
            timeout_seconds=timeout_seconds,
            expected_branch=expected_branch,
            launch_source=launch_source,
            prompt_version=prompt_version,
            capability_override=capability_override,
            repository_already_validated=repository_already_validated,
            workspace_verification=workspace_verification,
            executor_id=executor_id,
            provider_route_ids=route.providers,
            max_provider_attempts=route.max_attempts,
            provider_route_reason=route.selection_reason,
            provider_policy_version=route.policy_version,
            canonical_repository_path=project_config.get_project_config(project).get("repository_path"),
            max_global_concurrency=max_global_concurrency,
            untrusted=untrusted,
            operator_elevated=operator_elevated,
        )
        db.append_run_event(self.db_path, run["id"], "context_manifest", manifest)
        run = dict(run)
        run["context_manifest"] = manifest
        return run

    # ------------------------------------------------------------------
    # Listing / inspection
    # ------------------------------------------------------------------

    def list_tasks(self, *, project: str | None = None) -> list[dict]:
        return db.list_tasks(self.db_path, project=project)

    def delete_task(self, task_id: str) -> bool:
        """Remove a task's runtime.db footprint (session/run/event/report/
        completion cascade). Call alongside `tasks_repository.delete_task` so a
        deleted Kanban card leaves no orphan runtime rows (audit AR-1)."""
        return db.delete_task(self.db_path, task_id)

    def list_sessions(self, *, task_id: str | None = None) -> list[dict]:
        return db.list_sessions(self.db_path, task_id=task_id)

    def list_runs(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        state: str | None = None,
        states: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        runs = db.list_runs(
            self.db_path,
            session_id=session_id,
            task_id=task_id,
            state=state,
            states=states,
            limit=limit,
        )
        provenance_by_run = provenance.views_for_runs(
            self.db_path, [run["id"] for run in runs]
        )
        return [
            {**run, "provenance": provenance_by_run.get(run["id"])} for run in runs
        ]

    def count_runs(self, *, states: Iterable[str] | None = None) -> int:
        """Authoritative total run count without materializing rows (see
        `db.count_runs`). Use this for dashboard totals/denominators so the
        Live Board's `limit=200` window never silently under-counts."""
        return db.count_runs(self.db_path, states=states)

    def get_run(self, run_id: str) -> dict | None:
        run = db.get_run(self.db_path, run_id)
        if run is None:
            return None
        return {**run, "provenance": provenance.get_view(self.db_path, run_id)}

    def get_latest_run_for_task(self, task_id: str) -> dict | None:
        return db.get_latest_run_for_task(self.db_path, task_id)

    def get_events(self, run_id: str, *, after_seq: int = 0, limit: int = 1000) -> list[dict]:
        return db.list_run_events(self.db_path, run_id, after_seq=after_seq, limit=limit)

    def get_report(self, run_id: str) -> dict | None:
        return db.get_report(self.db_path, run_id)

    def get_reports_for_runs(self, run_ids: list[str]) -> dict[str, dict]:
        """Batch of `get_report` — one query for a board of runs (audit H5)."""
        return db.get_reports_for_runs(self.db_path, run_ids)

    # ------------------------------------------------------------------
    # Completion pipeline (read-only projections + bounded advance)
    # ------------------------------------------------------------------

    def get_completion(self, run_id: str) -> dict | None:
        return db.get_completion(self.db_path, run_id)

    def get_completions_for_runs(self, run_ids: list[str]) -> dict[str, dict]:
        """Batch of `get_completion` — one query for a board of runs (audit H5)."""
        return db.get_completions_for_runs(self.db_path, run_ids)

    def get_completion_by_task(self, task_id: str) -> dict | None:
        return db.get_completion_by_task(self.db_path, task_id)

    def get_completion_events(self, run_id: str, *, limit: int = 500) -> list[dict]:
        return db.list_completion_events(self.db_path, run_id, limit=limit)

    def get_validation_results(self, run_id: str) -> list[dict]:
        return db.list_validation_results(self.db_path, run_id)

    def advance_completions(self, *, now=None, limit: int = 50, github=None) -> list:
        """Advance every due, non-terminal completion row once. Delegates to the
        Supervisor; safe to call on demand (bounded)."""
        return self.supervisor.advance_completions(now=now, limit=limit, github=github)

    def request_manual_merge(
        self, run_id: str, *, confirmed: bool, github=None
    ) -> dict:
        """Merge one task explicitly; the completion service re-checks every
        gate and owns the global sequential merge slot."""
        from command_center.runtime.completion_service import CompletionOrchestrator

        return CompletionOrchestrator(
            self.db_path, github=github
        ).request_manual_merge(run_id, confirmed=confirmed)

    # ------------------------------------------------------------------
    # Autonomy proposals (AICC-AUTONOMY-002) — the pre-execution decision
    # layer. The engine governs recommendation -> approval -> execution but
    # never executes: `dispatch_proposal` only records the boundary crossing
    # and returns a dry-run plan the caller must run via `start_run` itself.
    # ------------------------------------------------------------------

    @property
    def autonomy(self) -> autonomy_service.AutonomyEngine:
        engine = getattr(self, "_autonomy_engine", None)
        if engine is None:
            engine = autonomy_service.AutonomyEngine(self.db_path)
            self._autonomy_engine = engine
        return engine

    def create_proposal(
        self,
        *,
        kind: str,
        project: str,
        title: str,
        rationale: str,
        evidence: list | None = None,
        task_id: str | None = None,
        policy: "autonomy.AutonomyPolicy | None" = None,
        parameters: dict | None = None,
    ) -> dict:
        return self.autonomy.create_proposal(
            kind=kind,
            project=project,
            title=title,
            rationale=rationale,
            evidence=evidence,
            task_id=task_id,
            policy=policy,
            parameters=parameters,
        )

    def assess_proposal(self, proposal_id: str, *, policy=None, now=None) -> dict:
        return self.autonomy.assess(proposal_id, policy=policy, now=now)

    def plan_proposal(self, proposal_id: str) -> dict:
        """Dry-run: describe what the proposal would do; execute nothing."""
        return self.autonomy.plan(proposal_id)

    def approve_proposal(self, proposal_id: str, *, actor: str, reason: str | None = None) -> dict:
        return self.autonomy.approve(proposal_id, actor=actor, reason=reason)

    def reject_proposal(self, proposal_id: str, *, actor: str, reason: str) -> dict:
        return self.autonomy.reject(proposal_id, actor=actor, reason=reason)

    def withdraw_proposal(self, proposal_id: str, *, actor: str, reason: str | None = None) -> dict:
        return self.autonomy.withdraw(proposal_id, actor=actor, reason=reason)

    def dispatch_proposal(self, proposal_id: str, *, actor: str, policy=None) -> dict:
        """Record that an APPROVED proposal has crossed the execution boundary
        and return its plan. Does not launch anything — the caller executes the
        plan explicitly via `start_run`, then calls `confirm_proposal_execution`."""
        return self.autonomy.dispatch(proposal_id, actor=actor, policy=policy)

    def confirm_proposal_execution(
        self, proposal_id: str, *, actor: str, run_id: str | None = None, task_id: str | None = None
    ) -> dict:
        return self.autonomy.confirm_execution(
            proposal_id, actor=actor, run_id=run_id, task_id=task_id
        )

    def get_proposal(self, proposal_id: str) -> dict | None:
        return self.autonomy.get(proposal_id)

    def list_proposals(self, *, project=None, states=None, kind=None, limit: int = 200) -> list[dict]:
        return self.autonomy.list(project=project, states=states, kind=kind, limit=limit)

    def get_proposal_evidence(self, proposal_id: str) -> list[dict]:
        return self.autonomy.evidence(proposal_id)

    def get_proposal_events(self, proposal_id: str, *, limit: int = 500) -> list[dict]:
        return self.autonomy.events(proposal_id, limit=limit)

    # ------------------------------------------------------------------
    # Cancellation — requires explicit confirmation from the caller/UI
    # ------------------------------------------------------------------

    def request_cancel(self, run_id: str, *, confirmed: bool, grace_seconds: float | None = None) -> dict:
        kwargs = {}
        if grace_seconds is not None:
            kwargs["grace_seconds"] = grace_seconds
        return self.supervisor.cancel(run_id, confirmed=confirmed, **kwargs)

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    def reconcile(self) -> list[dict]:
        return self.supervisor.reconcile()

    # ------------------------------------------------------------------
    # Scheduling (read-only decision layer — never launches anything here)
    # ------------------------------------------------------------------

    def plan_schedule(
        self,
        work_items: list[scheduler.WorkItem],
        *,
        registry: scheduler.AgentRegistry | None = None,
        config: scheduler.SchedulerConfig | None = None,
        policy: scheduler.RetryPolicy | None = None,
        now: str | None = None,
    ) -> scheduler.SchedulingPlan:
        """Deterministically plan which `work_items` should be assigned,
        deferred, or blocked *right now*, against the live in-flight load read
        from this API's own `runtime.db`. Pure decision only — this returns a
        `SchedulingPlan` and launches nothing. Acting on an `ASSIGN` decision
        is a separate, explicit `start_run` call by the caller, so the launch
        confirmation / sensitive-content boundary is never bypassed here."""
        load = scheduler.build_load_snapshot(self.db_path)
        return scheduler.plan(
            work_items,
            registry=registry or scheduler.default_registry(),
            load=load,
            config=config,
            policy=policy,
            now=now,
        )

    # ------------------------------------------------------------------
    # Context assembly (BANK/LEGAL sensitive-project boundary)
    # ------------------------------------------------------------------

    def assemble_context(
        self,
        *,
        project_id: str,
        metadata: dict | None = None,
        candidate_content: dict[str, str] | None = None,
        confirmed_items: list[str] | None = None,
    ) -> dict:
        return context_service.assemble_context(
            project_id=project_id,
            metadata=metadata,
            candidate_content=candidate_content,
            confirmed_items=confirmed_items,
        )
