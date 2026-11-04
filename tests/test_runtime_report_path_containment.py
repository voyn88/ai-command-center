"""MAJOR-9: the runtime report *write* path must stay under REPORTS_ROOT.

`command_center.runtime.reports.report_path_for` is a second copy of the report
path builder -- the `agent_runner` one was contained under SEC-2, this one was
not. It builds ``REPORTS_ROOT / project / f"{ts}_{id[:12]}.md"`` from a run's
``project``/``id``; an imported or hand-authored ``project`` like "../../root"
walked the write out of REPORTS_ROOT. Each user-influenced component is now
sanitized to a separator-free charset (mirrors SEC-2's fix on the sibling).
"""

from __future__ import annotations

from command_center.runtime import reports


def test_malicious_project_cannot_escape_reports_root(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_ROOT", tmp_path)
    run = {"id": "run-1", "project": "../../root", "started_at": "2026-07-29T00:00:00"}
    resolved = reports.report_path_for(run).resolve()
    assert tmp_path.resolve() in resolved.parents, f"escaped REPORTS_ROOT: {resolved}"


def test_malicious_run_id_cannot_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_ROOT", tmp_path)
    run = {"id": "../../../pwn", "project": "AICC", "started_at": "2026-07-29T00:00:00"}
    resolved = reports.report_path_for(run).resolve()
    assert tmp_path.resolve() in resolved.parents, f"escaped REPORTS_ROOT: {resolved}"
    # the sanitized id stays a single filename component under <project>/
    assert resolved.parent == (tmp_path / "AICC").resolve()


def test_legit_values_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_ROOT", tmp_path)
    run = {"id": "run-abc123", "project": "AICC-Proj", "started_at": "2026-07-29T12:00:00"}
    path = reports.report_path_for(run)
    assert path.parent == tmp_path / "AICC-Proj"
    assert path.name.startswith("20260729-120000_run-abc123")
    assert path.suffix == ".md"


def test_known_project_name_uses_canonical_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_ROOT", tmp_path)
    run = {
        "id": "run-abc123",
        "project": "  ai   command center ",
        "started_at": "2026-07-29T12:00:00",
    }

    assert reports.report_path_for(run).parent == tmp_path / "AICC"


def test_historical_noncanonical_reference_remains_resolvable(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_ROOT", tmp_path)
    historical = tmp_path / "AI_Command_Center" / "old.md"
    historical.parent.mkdir()
    historical.write_text("legacy", encoding="utf-8")

    assert (
        reports.resolve_report_path("reports/AI_Command_Center/old.md")
        == historical.resolve()
    )


def test_split_root_reference_survives_restart(tmp_path, monkeypatch):
    reports_root = tmp_path / "external-artifacts" / "reports"
    monkeypatch.setattr(reports, "REPORTS_ROOT", reports_root)
    path = reports_root / "AIOS" / "result.md"
    path.parent.mkdir(parents=True)
    path.write_text("verified result", encoding="utf-8")

    stored = reports.stored_report_path(path)
    assert stored == "reports/AIOS/result.md"
    # Resolution depends only on the persisted reference and configured root,
    # not on a process-local absolute path or the source checkout root.
    assert reports.resolve_report_path(stored) == path.resolve()


def test_report_resolver_rejects_traversal_and_symlink_escape(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    reports_root.mkdir()
    outside = tmp_path / "private.md"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(reports, "REPORTS_ROOT", reports_root)

    assert reports.resolve_report_path("reports/../../private.md") is None
    (reports_root / "escape.md").symlink_to(outside)
    assert reports.resolve_report_path("reports/escape.md") is None
