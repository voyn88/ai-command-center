"""Audit checks — the pluggable signal sources behind the Audit engine.

Each check implements :class:`command_center.audit.checks.base.Check`: a pure
producer of :class:`~command_center.audit.types.Finding` value objects. New
checks slot in by registering a factory with the
:class:`~command_center.audit.registry.CheckRegistry` — no change to the runner,
the write service or the API.
"""
