"""Operator CLI: autonomy-status | pause | resume, and one pipeline tick.

  python -m command_center.autonomy_cli status
  python -m command_center.autonomy_cli pause --reason 'operator stop'
  python -m command_center.autonomy_cli resume
  python -m command_center.autonomy_cli tick-once
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from command_center import autonomy_guard


def _root(ns: argparse.Namespace) -> Path:
    return Path(ns.root).expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autonomy_cli")
    parser.add_argument("--root", default=".", help="project root that holds data/")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    p_pause = sub.add_parser("pause")
    p_pause.add_argument("--reason", required=True)
    p_pause.add_argument("--sha", default=None)
    p_pause.add_argument("--actor", default="operator")
    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--actor", default="operator")
    sub.add_parser("tick-once")

    ns = parser.parse_args(argv)
    root = _root(ns)
    if ns.cmd == "status":
        print(json.dumps(autonomy_guard.status(root), ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "pause":
        state = autonomy_guard.pause(root, reason=ns.reason, actor=ns.actor, sha=ns.sha)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "resume":
        state = autonomy_guard.resume(root, actor=ns.actor)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "tick-once":
        from command_center.runtime.api import ExecutionCenterAPI
        from command_center import project_config, task_pipeline

        decision = autonomy_guard.check_dispatch(root, repo="local", kind="pr")
        if not decision.allowed:
            print(json.dumps({"status": "skipped", **decision.as_dict()}, ensure_ascii=False))
            return 0
        api = ExecutionCenterAPI(root)
        configs = project_config.load_all(root) if hasattr(project_config, "load_all") else {}
        if not isinstance(configs, dict):
            configs = {}
        result = task_pipeline.tick(root, api, configs)
        payload = result.as_dict() if hasattr(result, "as_dict") else {"result": str(result)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
