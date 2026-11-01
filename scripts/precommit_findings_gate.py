#!/usr/bin/env python3
"""Pre-commit gate for the Wave-2 Audit engine.

Installed as ``.git/hooks/pre-commit`` by ``scripts/install-git-hooks.sh``,
this runs the same five-category pass (security, lint, code-quality, deps,
coverage) a manual ``POST /audit/run`` or the runtime auto-trigger
(``scripts/findings_auto_trigger.py``) use, over the working tree — tracking
every finding in the same store — and blocks the commit (non-zero exit) when
a high/critical finding survives. The point is catching and recording an
anti-pattern *before* it reaches a commit, not after a nightly or manual pass
happens to notice it.

``deps`` and ``coverage`` are excluded from the default check set: ``deps``
only reacts to requirements-file edits and ``coverage`` reads a prior test
run's data, neither of which a typical commit changes, and both are slower
than the ruff-backed checks. Pass ``--checks`` to widen or narrow that.

Usage::

    python scripts/precommit_findings_gate.py
    python scripts/precommit_findings_gate.py --project AICC --fail-on critical
    git commit --no-verify   # bypass for a deliberate exception
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from command_center.api import audit_schemas as a  # noqa: E402
from command_center.api import audit_service  # noqa: E402
from command_center.api.models import AuditFinding  # noqa: E402

#: Checks fast enough to run on every commit (see module docstring for why
#: ``deps``/``coverage`` are excluded by default).
DEFAULT_PRECOMMIT_CHECKS: tuple[str, ...] = ("security", "lint", "code-quality")

#: Severities that block the commit outright. Anything lower is still tracked
#: (persisted, visible in the findings inbox) but does not stop ``git commit``.
DEFAULT_BLOCKING_SEVERITIES: frozenset[str] = frozenset({"high", "critical"})


def blocking_findings(
    findings: list[AuditFinding], severities: frozenset[str]
) -> list[AuditFinding]:
    """The subset of ``findings`` whose severity should block the commit."""
    return [f for f in findings if f.severity in severities]


def _format_finding(finding: AuditFinding, *, blocking: bool) -> str:
    marker = "BLOCK" if blocking else "info "
    location = f"{finding.file_path}:{finding.loc}" if finding.file_path else "-"
    return f"[{marker}] {finding.category:12s} {finding.severity:8s} {location} {finding.summary}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="AICC")
    parser.add_argument("--checks", nargs="*", default=list(DEFAULT_PRECOMMIT_CHECKS))
    parser.add_argument(
        "--fail-on",
        nargs="*",
        default=sorted(DEFAULT_BLOCKING_SEVERITIES),
        help="Severities that block the commit (default: high critical).",
    )
    args = parser.parse_args(argv)
    blocking_severities = frozenset(args.fail_on)

    payload = a.AuditRunRequest(project=args.project, checks=args.checks or None)
    try:
        result = audit_service.run_audit(payload)
    except audit_service.SensitiveProjectRefError:
        print(f"pre-commit audit: {args.project!r} is a sensitive project; skipped")
        return 0
    except KeyError as exc:
        print(f"pre-commit audit: {exc}", file=sys.stderr)
        return 1

    offenders = blocking_findings(result.findings, blocking_severities)
    for finding in result.findings:
        print(_format_finding(finding, blocking=finding in offenders))

    if offenders:
        print(
            f"\npre-commit audit: {len(offenders)} finding(s) at "
            f"{sorted(blocking_severities)} severity — commit blocked. Fix, "
            "resolve the finding, or `git commit --no-verify` to override.",
            file=sys.stderr,
        )
        return 1

    print(f"pre-commit audit: {len(result.findings)} finding(s) tracked, none blocking")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
