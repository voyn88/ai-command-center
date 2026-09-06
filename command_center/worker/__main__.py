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
import sys


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    from command_center.db import pool
    from command_center.db.config import ConfigError, load_config
    from command_center.db.work_queue_store import WorkQueueStore
    from command_center.worker.credential_file import read_environment_file
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

    def reload_credentials() -> None:
        path = os.environ.get("AICC_WORKER_ENV_FILE", "")
        if not path:
            raise ConfigError("AICC_WORKER_ENV_FILE is required for credential reload")
        values = dict(os.environ)
        values.update(read_environment_file(path))
        pool.replace_pool(load_config(values))

    daemon = WorkerDaemon(
        WorkQueueStore(),
        handlers=build_handlers(),
        reload_credentials=reload_credentials,
    )
    daemon.install_signal_handlers()
    try:
        daemon.run_forever()
    finally:
        pool.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
