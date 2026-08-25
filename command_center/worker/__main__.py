"""Operator entry point: ``python -m command_center.worker``.

Mirrors ``python -m command_center.db``'s shape. The daemon's identity is its
DSN: ``AICC_PG_USER`` must be the host's enrolled ``aicc_w_*`` role, and
enrolment itself happens before this service exists (see the package
docstring). Exit codes are systemd's interface: non-zero means "restart me
with backoff", which is why auth failure exits instead of retrying.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from command_center.observability import CONTENT_TYPE, WorkerTelemetry


def _start_metrics(telemetry: WorkerTelemetry) -> ThreadingHTTPServer | None:
    host = os.environ.get("AICC_WORKER_METRICS_HOST", "127.0.0.1")
    port = int(os.environ.get("AICC_WORKER_METRICS_PORT", "9108"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- stdlib handler contract
            if self.path != "/metrics":
                self.send_error(404)
                return
            body = telemetry.render().encode()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            return

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as error:
        logging.getLogger(__name__).error("worker metrics listener failed: %s", error)
        return None
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    return server


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    from command_center.db import pool
    from command_center.db.config import ConfigError, load_config
    from command_center.db.work_queue_store import WorkQueueStore
    from command_center.worker.daemon import WorkerDaemon
    from command_center.worker.handlers import build_handlers

    try:
        config = load_config()
    except ConfigError as error:
        print(f"worker: configuration refused: {error}", file=sys.stderr)
        return 2
    try:
        pool.open_pool(config)
    except Exception as error:  # noqa: BLE001 -- startup boundary
        print(f"worker: cannot reach PostgreSQL: {error}", file=sys.stderr)
        return 3

    telemetry = WorkerTelemetry(
        worker=os.environ.get("AICC_WORKER_ID", socket.gethostname()),
        cost_per_hour=float(os.environ.get("AICC_WORKER_COST_PER_HOUR", "0")),
    )
    metrics_server = _start_metrics(telemetry)
    daemon = WorkerDaemon(
        WorkQueueStore(), handlers=build_handlers(), telemetry=telemetry
    )
    daemon.install_signal_handlers()
    try:
        daemon.run_forever()
    finally:
        if metrics_server is not None:
            metrics_server.shutdown()
        pool.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
