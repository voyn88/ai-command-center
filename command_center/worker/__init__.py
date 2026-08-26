"""The headless worker: a systemd-run daemon that claims and executes work.

Scope note, stated where the next reader will look. This package delivers the
*service*: the claim loop, lease heartbeats, graceful shutdown, refusal
handling and the systemd contract. What it deliberately does not deliver is
the bridge from a claimed payload to a real agent run — the payload schema for
the ``execution`` queue is undefined and its producer is unbuilt, so a bridge
written now would be a guess about a contract that does not exist. Handlers
are a registry precisely so that bridge can arrive as its own reviewed change.
"""

from command_center.worker.daemon import WorkerDaemon

__all__ = ["WorkerDaemon"]
