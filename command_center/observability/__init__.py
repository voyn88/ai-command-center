"""Cross-cutting tracing and SLO alerting for the backlog pipeline.

See :mod:`command_center.observability.trace` for the trace_id that follows
one backlog task from planning through merge, and
:mod:`command_center.observability.slo` for the programmatic invariant
checks that alert when the pipeline itself misbehaves.
"""

from __future__ import annotations
