"""The backlog orchestrator (VOYN-W0-BACKLOG-ORCHESTRATOR BO-S2/S2a).

Consumer of two accepted authorities, owner of neither: the structured
backlog store (BO-S1, 0005) decides what a task IS; the work queue
(SRV-04b/05/06, 0002) decides how execution is delivered. This package only
composes them — candidate selection, the executor cascade, the plan report —
and every act that must be atomic is a SQL function (0006), not Python.
"""
