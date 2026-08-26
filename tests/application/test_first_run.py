"""Unit tests for the first-run setup services (D-1).

All probes are injected — no real subprocess, network, or user-home access.
"""

from __future__ import annotations

import os

import pytest

from command_center.application.first_run import (
    HealthStatus,
    blocking_errors,
    humanize_startup_error,
    initialize_workspace,
    run_health_checks,
    validate_workspace_root,
    workspace_is_configured,
)


def _which_factory(available: dict[str, str]):
    return lambda name: available.get(name)


ALL_TOOLS = {
    "git": "/usr/bin/git",
    "gh": "/opt/homebrew/bin/gh",
    "uv": "/opt/homebrew/bin/uv",
    "docker": "/usr/local/bin/docker",
}


class TestHealthChecks:
    def test_everything_available_is_all_ok(self, tmp_path):
        hosts = tmp_path / "hosts.yml"
        hosts.write_text("github.com:\n  user: octocat\n", encoding="utf-8")
        items = run_health_checks(
            which=_which_factory(ALL_TOOLS),
            network_probe=lambda: True,
            gh_hosts_path=hosts,
        )
        assert [item.check_id for item in items] == [
            "git", "gh", "uv", "docker", "network",
        ]
        assert all(item.status is HealthStatus.OK for item in items)
        assert not blocking_errors(items)

    def test_missing_required_tools_are_errors_with_fix_hints(self, tmp_path):
        items = run_health_checks(
            which=_which_factory({}),
            network_probe=lambda: True,
            gh_hosts_path=tmp_path / "absent.yml",
        )
        by_id = {item.check_id: item for item in items}
        for required in ("git", "gh", "uv"):
            assert by_id[required].status is HealthStatus.ERROR
            assert by_id[required].fix_hint
        assert {item.check_id for item in blocking_errors(items)} == {
            "git", "gh", "uv",
        }

    def test_docker_and_network_failures_warn_but_do_not_block(self, tmp_path):
        hosts = tmp_path / "hosts.yml"
        hosts.write_text("github.com:\n", encoding="utf-8")
        items = run_health_checks(
            which=_which_factory({k: v for k, v in ALL_TOOLS.items() if k != "docker"}),
            network_probe=lambda: False,
            gh_hosts_path=hosts,
        )
        by_id = {item.check_id: item for item in items}
        assert by_id["docker"].status is HealthStatus.WARNING
        assert by_id["network"].status is HealthStatus.WARNING
        assert not blocking_errors(items)

    def test_gh_installed_but_unauthenticated_warns_with_login_hint(self, tmp_path):
        items = run_health_checks(
            which=_which_factory(ALL_TOOLS),
            network_probe=lambda: True,
            gh_hosts_path=tmp_path / "no-hosts.yml",
        )
        gh = {item.check_id: item for item in items}["gh"]
        assert gh.status is HealthStatus.WARNING
        assert "gh auth login" in (gh.fix_hint or "")

    def test_network_probe_exception_is_treated_as_offline(self, tmp_path):
        def broken() -> bool:
            raise OSError("dns down")

        hosts = tmp_path / "hosts.yml"
        hosts.write_text("github.com:\n", encoding="utf-8")
        items = run_health_checks(
            which=_which_factory(ALL_TOOLS),
            network_probe=broken,
            gh_hosts_path=hosts,
        )
        network = {item.check_id: item for item in items}["network"]
        assert network.status is HealthStatus.WARNING

    def test_gh_hosts_lookalike_host_does_not_count_as_authenticated(self, tmp_path):
        # Spoof / look-alike hosts must not satisfy the anchored key match.
        hosts = tmp_path / "hosts.yml"
        hosts.write_text(
            "evil-github.com:\n  user: mallory\n"
            "github.com.evil.io:\n  user: mallory\n"
            "notgithub.com:\n  user: mallory\n",
            encoding="utf-8",
        )
        items = run_health_checks(
            which=_which_factory(ALL_TOOLS),
            network_probe=lambda: True,
            gh_hosts_path=hosts,
        )
        gh = {item.check_id: item for item in items}["gh"]
        assert gh.status is HealthStatus.WARNING

    def test_gh_hosts_subdomain_of_github_counts(self, tmp_path):
        hosts = tmp_path / "hosts.yml"
        hosts.write_text("api.github.com:\n  user: octocat\n", encoding="utf-8")
        items = run_health_checks(
            which=_which_factory(ALL_TOOLS),
            network_probe=lambda: True,
            gh_hosts_path=hosts,
        )
        gh = {item.check_id: item for item in items}["gh"]
        assert gh.status is HealthStatus.OK


class TestWorkspaceValidation:
    def test_empty_input_is_rejected(self):
        assert validate_workspace_root("   ") is not None

    def test_relative_path_is_rejected(self):
        assert "абсолютн" in validate_workspace_root("relative/path")

    def test_existing_file_is_rejected(self, tmp_path):
        file_path = tmp_path / "occupied"
        file_path.write_text("x", encoding="utf-8")
        assert "файл" in validate_workspace_root(str(file_path))

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
    def test_unwritable_parent_is_rejected(self, tmp_path):
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        sealed.chmod(0o500)
        try:
            error = validate_workspace_root(str(sealed / "workspace"))
        finally:
            sealed.chmod(0o700)
        assert "запись" in (error or "")

    def test_nonexistent_but_creatable_path_is_accepted(self, tmp_path):
        assert validate_workspace_root(str(tmp_path / "new" / "workspace")) is None


class TestInitializeWorkspace:
    def test_creates_layout_and_exports_environment(self, tmp_path):
        env: dict[str, str] = {}
        root = initialize_workspace(str(tmp_path / "workspace"), environ=env)
        for child in ("data", "generated", "reports"):
            assert (root / child).is_dir()
        assert env["AICC_WORKSPACE_ROOT"] == str(root)
        assert env["AICC_DATA_DIR"] == str(root / "data")
        assert env["AICC_GENERATED_ROOT"] == str(root / "generated")
        assert env["AICC_REPORTS_ROOT"] == str(root / "reports")

    def test_invalid_input_raises_the_human_message(self):
        with pytest.raises(ValueError, match="абсолютным"):
            initialize_workspace("not/absolute", environ={})

    def test_idempotent_over_an_existing_workspace(self, tmp_path):
        env: dict[str, str] = {}
        first = initialize_workspace(str(tmp_path / "ws"), environ=env)
        second = initialize_workspace(str(tmp_path / "ws"), environ=env)
        assert first == second


class TestWorkspaceIsConfigured:
    def test_explicit_root_pointing_at_directory(self, tmp_path):
        assert workspace_is_configured(
            {"AICC_WORKSPACE_ROOT": str(tmp_path)}, home=tmp_path / "home"
        )

    def test_explicit_root_pointing_nowhere_is_not_configured(self, tmp_path):
        env = {"AICC_WORKSPACE_ROOT": str(tmp_path / "gone")}
        assert not workspace_is_configured(env, home=tmp_path / "home")

    def test_data_dir_override_counts(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        assert workspace_is_configured(
            {"AICC_DATA_DIR": str(data)}, home=tmp_path / "home"
        )

    def test_conventional_home_directory_counts(self, tmp_path):
        (tmp_path / "Projects" / "ai-command-center" / "data").mkdir(parents=True)
        assert workspace_is_configured({}, home=tmp_path)

    def test_pristine_machine_is_not_configured(self, tmp_path):
        assert not workspace_is_configured({}, home=tmp_path)


class TestHumanizeStartupError:
    def test_missing_data_dir_maps_to_workspace_message(self):
        failure = humanize_startup_error(FileNotFoundError("data/tasks.json"))
        assert "пространство" in failure.title
        assert "AICC_WORKSPACE_ROOT" in failure.hint

    def test_unopenable_database_maps_to_workspace_message(self):
        failure = humanize_startup_error(
            RuntimeError("unable to open database file")
        )
        assert "пространство" in failure.title

    def test_aios_backend_failure_maps_to_aios_message(self):
        class AIOSStatusError(Exception):
            pass

        failure = humanize_startup_error(AIOSStatusError("connection refused"))
        assert "AIOS" in failure.title

    def test_gh_auth_failure_maps_to_gh_message(self):
        failure = humanize_startup_error(
            RuntimeError("gh: To get started with GitHub CLI, please run: gh auth login")
        )
        assert "GitHub" in failure.title
        assert "gh auth login" in failure.hint

    def test_unknown_error_falls_back_to_generic_actionable_message(self):
        failure = humanize_startup_error(ZeroDivisionError("boom"))
        assert failure.title
        assert failure.hint
