"""E-Советник — the advisor / insights engine.

A pluggable, registry-based engine that turns local, free signals (run history,
task/failure history) into typed ``advisor_proposal`` cards and, under
conservative config-driven auto-rules, can promote them into board tasks.

Public surface::

    Collector, CollectorContext, Candidate      -- the collector interface
    ProposalScorer, Score                        -- value/effort/risk scoring
    CollectorRegistry, default_registry          -- the extension point
    AdvisorConfig, AutoRule                       -- draft-vs-promote policy
    AdvisorService, RunSummary                    -- the orchestrator
    run_pass                                      -- application entry (API/CLI)

The engine persists exclusively through the Wave-1 write service, so it reuses
the audited single-writer and event-publishing seams rather than opening any
store itself.
"""

from __future__ import annotations

from command_center.advisor.api import run_pass
from command_center.advisor.collectors import (
    Collector,
    CompetitorCollector,
    ExternalSignalCollector,
    ExternalSignalSource,
    FeedbackCollector,
    OptimizationCollector,
    TrendCollector,
    UxCollector,
)
from command_center.advisor.config import AdvisorConfig, AutoRule, DEFAULT_CONFIG
from command_center.advisor.registry import CollectorRegistry, default_registry
from command_center.advisor.scorer import ProposalScorer, Score
from command_center.advisor.service import (
    AdvisorService,
    ProposalOutcome,
    RunSummary,
)
from command_center.advisor.types import Candidate, CollectorContext

__all__ = [
    "Collector",
    "Candidate",
    "CollectorContext",
    "ProposalScorer",
    "Score",
    "CollectorRegistry",
    "default_registry",
    "AdvisorConfig",
    "AutoRule",
    "DEFAULT_CONFIG",
    "AdvisorService",
    "ProposalOutcome",
    "RunSummary",
    "run_pass",
    "OptimizationCollector",
    "FeedbackCollector",
    "ExternalSignalCollector",
    "ExternalSignalSource",
    "TrendCollector",
    "CompetitorCollector",
    "UxCollector",
]
