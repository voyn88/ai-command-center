"""Unit tests for the dependency-based test-impact selector.

These exercise the pure graph logic on a synthetic in-memory module tree, so
they need neither a git repository nor the real source tree, and they run in
milliseconds as part of the normal suite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("select_tests.py")
_spec = importlib.util.spec_from_file_location("select_tests", _MODULE_PATH)
assert _spec and _spec.loader
select_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(select_tests)


def test_module_name_for_maps_paths():
    assert select_tests.module_name_for("command_center/agent_runner.py") == (
        "command_center.agent_runner"
    )
    assert select_tests.module_name_for("command_center/__init__.py") == (
        "command_center"
    )
    assert select_tests.module_name_for("app.py") == "app"
    assert select_tests.module_name_for("README.md") is None


def _sample_graph():
    # forward: module -> modules it imports (first-party only)
    forward = {
        "command_center.core": set(),
        "command_center.feature_a": {"command_center.core"},
        "command_center.feature_b": set(),
        "tests.test_core": {"command_center.core"},
        "tests.test_feature_a": {"command_center.feature_a"},
        "tests.test_feature_b": {"command_center.feature_b"},
    }
    module_to_path = {
        "command_center.core": "command_center/core.py",
        "command_center.feature_a": "command_center/feature_a.py",
        "command_center.feature_b": "command_center/feature_b.py",
        "tests.test_core": "tests/test_core.py",
        "tests.test_feature_a": "tests/test_feature_a.py",
        "tests.test_feature_b": "tests/test_feature_b.py",
    }
    reverse = select_tests.invert(forward)
    return module_to_path, reverse


def test_leaf_change_selects_only_direct_dependents():
    module_to_path, reverse = _sample_graph()
    selected, trigger_all = select_tests.select(
        ["command_center/feature_b.py"], module_to_path, reverse
    )
    assert trigger_all is False
    assert selected == {"tests/test_feature_b.py"}


def test_transitive_change_selects_downstream_tests():
    module_to_path, reverse = _sample_graph()
    # core is imported by feature_a and test_core; feature_a is imported by
    # test_feature_a. Changing core must reach both test files.
    selected, trigger_all = select_tests.select(
        ["command_center/core.py"], module_to_path, reverse
    )
    assert trigger_all is False
    assert selected == {"tests/test_core.py", "tests/test_feature_a.py"}


def test_changed_test_file_selects_itself():
    module_to_path, reverse = _sample_graph()
    selected, trigger_all = select_tests.select(
        ["tests/test_feature_a.py"], module_to_path, reverse
    )
    assert trigger_all is False
    assert selected == {"tests/test_feature_a.py"}


def test_conftest_triggers_full_suite():
    module_to_path, reverse = _sample_graph()
    _, trigger_all = select_tests.select(
        ["tests/conftest.py"], module_to_path, reverse
    )
    assert trigger_all is True


def test_pyproject_triggers_full_suite():
    module_to_path, reverse = _sample_graph()
    _, trigger_all = select_tests.select(
        ["pyproject.toml"], module_to_path, reverse
    )
    assert trigger_all is True


def test_unmapped_python_file_triggers_full_suite():
    module_to_path, reverse = _sample_graph()
    # A first-party-looking python path that is not in the graph is ambiguous,
    # so the selector must widen to the full suite rather than under-select.
    _, trigger_all = select_tests.select(
        ["command_center/brand_new_module.py"], module_to_path, reverse
    )
    assert trigger_all is True


def test_a_ci_workflow_change_triggers_full_suite():
    """VOYN-W0-AICC-CI-IMPACT-SELECTION-REQUIRED-GATE: before
    `.github/workflows/` joined TRIGGER_ALL_PREFIXES, a workflow file (not
    `.py`, not first-party) fell through `select()`'s `continue` branch --
    silently unaccounted for, neither selecting anything nor widening.
    Harmless while this selector only backed an advisory job; not harmless
    once its output narrows the REQUIRED gate."""
    module_to_path, reverse = _sample_graph()
    _, trigger_all = select_tests.select(
        [".github/workflows/ci.yml"], module_to_path, reverse
    )
    assert trigger_all is True


def test_docs_only_change_selects_nothing_and_does_not_trigger_all():
    module_to_path, reverse = _sample_graph()
    selected, trigger_all = select_tests.select(
        ["docs/guide.md"], module_to_path, reverse
    )
    assert trigger_all is False
    assert selected == set()


def test_real_tree_leaf_module_selects_a_small_subset():
    """Smoke test against the real repository graph (if present)."""
    root = select_tests.repo_root()
    if not (root / "command_center" / "aml_store.py").exists():
        return  # not in the product repo; skip silently
    module_to_path, forward = select_tests.build_graph(root)
    reverse = select_tests.invert(forward)
    selected, trigger_all = select_tests.select(
        ["command_center/aml_store.py"], module_to_path, reverse
    )
    assert trigger_all is False
    # A leaf store module must select at least its own test and stay far below
    # the full suite (~225 test files); this guards against a graph regression
    # that would make every change trigger-all.
    assert 0 < len(selected) < 60
