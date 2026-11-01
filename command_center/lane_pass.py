"""One operator pass: guard, arm pipeline settings, tick, deploy.

Composes existing pieces. Not a second scheduler.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from command_center import lane_budget, pipeline_settings


def run_once(root: Path, *, repo: str = "ai-command-center") -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    report: dict[str, Any] = {"root": str(root), "repo": repo, "steps": []}

    guard = lane_budget.check_dispatch(root, repo=repo, kind="pr")
    report["guard"] = guard.as_dict()
    if not guard.allowed:
        report["status"] = "skipped"
        return report

    settings = pipeline_settings.update_settings(
        root,
        actor="lane_pass",
        enabled=True,
        auto_launch=True,
        auto_merge_after_checks=True,
        require_independent_review=False,
    )
    report["settings"] = {
        "enabled": settings.enabled,
        "auto_launch": settings.auto_launch,
        "auto_merge_after_checks": settings.auto_merge_after_checks,
        "auto_merge_active": settings.auto_merge_active,
    }
    report["steps"].append("pipeline_armed")

    tick_payload: dict[str, Any] = {}
    try:
        from command_center import project_config, task_pipeline
        from command_center.runtime.api import ExecutionCenterAPI

        api = ExecutionCenterAPI(root)
        configs = project_config.load_all(root) if hasattr(project_config, "load_all") else {}
        if not isinstance(configs, dict):
            configs = {}
        result = task_pipeline.tick(root, api, configs)
        tick_payload = result.as_dict() if hasattr(result, "as_dict") else {"result": str(result)}
        report["steps"].append("pipeline_tick")
    except Exception as exc:  # noqa: BLE001
        tick_payload = {"error": f"{type(exc).__name__}: {exc}"}
        report["steps"].append("pipeline_tick_failed")
    report["tick"] = tick_payload

    deploy_payload: dict[str, Any] = {}
    try:
        from command_center.deployment.self_deploy import SelfDeployConfig, self_deploy_once

        deploy_guard = lane_budget.check_dispatch(root, repo=repo, kind="remediation")
        if not deploy_guard.allowed:
            deploy_payload = {"status": "skipped", **deploy_guard.as_dict()}
        else:
            deploy_report = self_deploy_once(str(root), SelfDeployConfig())
            deploy_payload = {
                "outcome": deploy_report.outcome,
                "detail": deploy_report.detail,
                "previous_sha": deploy_report.previous_sha,
                "target_sha": deploy_report.target_sha,
                "steps": list(deploy_report.steps),
            }
            if deploy_report.outcome in {"rolled_back", "failed"} and deploy_report.target_sha:
                lane_budget.pause_for_red_smoke(root, sha=deploy_report.target_sha)
                report["steps"].append("paused_red_smoke")
            elif deploy_report.outcome in {"deployed", "noop"}:
                lane_budget.record_event(root, repo, "remediation", failed=False)
            report["steps"].append(f"self_deploy_{deploy_report.outcome}")
    except Exception as exc:  # noqa: BLE001
        deploy_payload = {"error": f"{type(exc).__name__}: {exc}"}
        report["steps"].append("self_deploy_failed")
    report["deploy"] = deploy_payload
    report["status"] = "ran"
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="lane_pass")
    parser.add_argument("--root", default=".")
    parser.add_argument("--repo", default="ai-command-center")
    ns = parser.parse_args(argv)
    payload = run_once(Path(ns.root), repo=ns.repo)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") in {"ran", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
