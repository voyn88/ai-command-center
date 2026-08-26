"""Built-in advisor collectors and the shared :class:`Collector` interface."""

from __future__ import annotations

from command_center.advisor.collectors.base import Collector
from command_center.advisor.collectors.external import (
    CompetitorCollector,
    ExternalSignalCollector,
    ExternalSignalSource,
    TrendCollector,
    UxCollector,
)
from command_center.advisor.collectors.feedback import FeedbackCollector
from command_center.advisor.collectors.optimization import OptimizationCollector

__all__ = [
    "Collector",
    "OptimizationCollector",
    "FeedbackCollector",
    "ExternalSignalCollector",
    "ExternalSignalSource",
    "TrendCollector",
    "CompetitorCollector",
    "UxCollector",
]
