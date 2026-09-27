"""Operator CLI for lane_budget pause/resume and one pass."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from command_center import lane_budget


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lane_budget_cli")
    parser.add_argument("--root", default=".")
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
    root = Path(ns.root).expanduser().resolve()
    if ns.cmd == "status":
        print(json.dumps(lane_budget.status(root), ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "pause":
        print(json.dumps(lane_budget.pause(root, reason=ns.reason, actor=ns.actor, sha=ns.sha), ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "resume":
        print(json.dumps(lane_budget.resume(root, actor=ns.actor), ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "tick-once":
        from command_center.lane_pass import run_once

        payload = run_once(root)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("status") in {"ran", "skipped"} else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
