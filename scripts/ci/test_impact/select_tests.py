#!/usr/bin/env python3
"""Dependency-based test-impact selector for the AI Command Center CI.

Given a set of changed files (from a PR diff), this tool selects the subset of
test files whose behaviour could be affected by the change, by walking a static
Python import graph.  It powers the *advisory* fast pre-check job in CI; the
full ``pytest`` suite still runs as the required merge gate, so this selector
can never cause coverage loss — at worst it selects too few tests for the fast
job, which the full gate then catches anyway.

How the mapping is built
------------------------
1.  Every ``*.py`` file under the first-party roots (``command_center/``,
    ``tests/`` and the top-level ``app.py``) is parsed with the standard-library
    :mod:`ast` module.  No code is imported or executed.
2.  Each file is assigned its dotted module name from its path
    (``command_center/agent_runner.py`` -> ``command_center.agent_runner``).
3.  ``import`` / ``from ... import ...`` statements are resolved against that
    module table to build a directed graph "file X depends on first-party
    module Y".  Third-party and stdlib imports are ignored.
4.  The graph is inverted into a reverse-dependency map (module -> files that
    depend on it, transitively) so that from any changed source file we can find
    every test file that reaches it.

Selection rules
---------------
*   A changed **test file** always selects itself.
*   A changed **source file** selects every test file that transitively imports
    it.
*   A changed file that cannot be mapped into the graph, or that is a global
    dependency (any ``conftest.py``, ``pyproject.toml``, a requirements lock,
    or anything under ``scripts/ci/test_impact/`` itself), is treated as a
    *trigger-all*: the selector reports that the full suite must run.  This is
    the safe default — ambiguity widens the selection, never narrows it.

The reverse map is derived on every invocation from the current tree, so it is
always in sync with the code; there is no committed cache to update.

Usage
-----
    python scripts/ci/test_impact/select_tests.py            # diff vs origin/main
    python scripts/ci/test_impact/select_tests.py --base HEAD~1
    python scripts/ci/test_impact/select_tests.py --files a.py b.py
    python scripts/ci/test_impact/select_tests.py --format pytest

Exit code is always 0 on success; the caller inspects stdout (or ``--output``).
When a trigger-all file is touched, the single token ``ALL`` is printed (unless
``--format pytest``, which then prints ``tests`` — i.e. the whole suite).
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections import deque
from pathlib import Path

# First-party roots that participate in the import graph.
SOURCE_ROOTS = ("command_center",)
TEST_ROOT = "tests"
TOP_LEVEL_MODULES = ("app.py",)

# Touching any of these forces the full suite (they can affect every test).
TRIGGER_ALL_NAMES = {
    "pyproject.toml",
    "conftest.py",
    "requirements-ci-linux.lock",
    "requirements-ci-windows.lock",
    "requirements-dev.txt",
    "requirements.txt",
}
TRIGGER_ALL_PREFIXES = (
    "scripts/ci/test_impact/",
    # VOYN-W0-AICC-CI-IMPACT-SELECTION-REQUIRED-GATE (found live 2026-08-23,
    # wiring this selector into the REQUIRED `quality-gates` job for the
    # first time): `select()`'s own `is_first_party` fallback only trigger-
    # alls a changed file outside the graph when it ends in `.py` --
    # anything else outside the graph (including `.yml`) is silently
    # `continue`d, neither selected nor widened. That was harmless while
    # this selector only backed the *advisory* `impact-fast-check` job (a
    # CI workflow change there just meant "the fast job didn't notice,"
    # and the full required gate still covered it unconditionally). It
    # stopped being harmless the moment this selector's own output could
    # narrow the required gate: a PR touching `.github/workflows/` AND some
    # first-party file with a narrow test footprint would have its CI
    # config change go completely unaccounted for. Workflow changes are
    # exactly the release/deployment-adjacent class CLAUDE.md's own
    # portfolio rules already name for automatic full-matrix escalation.
    ".github/workflows/",
)


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def changed_files(base: str, root: Path) -> list[str]:
    """Return repo-relative paths changed vs ``base`` (three-dot / merge-base)."""
    merge_base = subprocess.run(
        ["git", "merge-base", base, "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    ref = merge_base.stdout.strip() if merge_base.returncode == 0 else base
    diff = subprocess.run(
        ["git", "diff", "--name-only", f"{ref}...HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    files = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    # Include not-yet-committed changes too, so the selector is useful locally.
    status = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    files.extend(line.strip() for line in status.stdout.splitlines() if line.strip())
    return sorted(set(files))


def module_name_for(rel_path: str) -> str | None:
    """Map a repo-relative ``*.py`` path to its dotted module name."""
    if not rel_path.endswith(".py"):
        return None
    parts = rel_path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def is_first_party(rel_path: str) -> bool:
    if rel_path in TOP_LEVEL_MODULES:
        return True
    top = rel_path.split("/", 1)[0]
    return top in SOURCE_ROOTS or top == TEST_ROOT


def collect_python_files(root: Path) -> list[str]:
    files: list[str] = []
    for base in (*SOURCE_ROOTS, TEST_ROOT):
        for path in (root / base).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            files.append(str(path.relative_to(root)))
    for top in TOP_LEVEL_MODULES:
        if (root / top).exists():
            files.append(top)
    return sorted(set(files))


def _resolve_relative(module: str | None, level: int, current_pkg: list[str]) -> str:
    """Resolve a ``from . import x`` style dotted target to an absolute module."""
    base = current_pkg[: len(current_pkg) - (level - 1)] if level > 0 else current_pkg
    parts = list(base)
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def parse_dependencies(
    rel_path: str, root: Path, known_modules: set[str]
) -> set[str]:
    """Return the set of first-party modules directly imported by ``rel_path``."""
    try:
        source = (root / rel_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel_path)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()

    self_module = module_name_for(rel_path) or ""
    current_pkg = self_module.split(".")[:-1] if self_module else []
    deps: set[str] = set()

    def record(dotted: str) -> None:
        # Record the longest known-module prefix of the dotted target, so that
        # ``from command_center.agent_runner import run`` maps onto the module
        # ``command_center.agent_runner`` even though ``run`` is an attribute.
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            if candidate in known_modules:
                deps.add(candidate)
                return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                target = _resolve_relative(node.module, node.level, current_pkg)
            else:
                target = node.module or ""
            if not target:
                continue
            record(target)
            # A `from pkg import submodule` where submodule is itself a module.
            for alias in node.names:
                record(f"{target}.{alias.name}")
    deps.discard(self_module)
    return deps


def build_graph(root: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Return (module -> path) and (module -> set of modules it depends on)."""
    files = collect_python_files(root)
    module_to_path: dict[str, str] = {}
    for rel in files:
        name = module_name_for(rel)
        if name:
            module_to_path[name] = rel
    known = set(module_to_path)

    forward: dict[str, set[str]] = {}
    for name, rel in module_to_path.items():
        forward[name] = parse_dependencies(rel, root, known)
    return module_to_path, forward


def invert(forward: dict[str, set[str]]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {m: set() for m in forward}
    for module, deps in forward.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(module)
    return reverse


def dependents_of(seed: str, reverse: dict[str, set[str]]) -> set[str]:
    """All modules that transitively depend on ``seed`` (including itself)."""
    seen: set[str] = set()
    queue: deque[str] = deque([seed])
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        for dependent in reverse.get(node, ()):  # who imports `node`
            if dependent not in seen:
                queue.append(dependent)
    return seen


def select(
    files: list[str],
    module_to_path: dict[str, str],
    reverse: dict[str, set[str]],
) -> tuple[set[str], bool]:
    """Return (selected test files, trigger_all)."""
    selected: set[str] = set()
    for rel in files:
        name = Path(rel).name
        if name in TRIGGER_ALL_NAMES or any(
            rel.startswith(p) for p in TRIGGER_ALL_PREFIXES
        ):
            return set(), True
        if not is_first_party(rel):
            # A changed file outside the graph (docs, web/, workflows, other
            # scripts). Docs-only changes are handled by the workflow's own
            # docs-only gate; here we conservatively trigger the full suite so
            # the fast job never gives false confidence for un-modelled files.
            if rel.endswith(".py"):
                return set(), True
            continue
        module = module_name_for(rel)
        if module is None or module not in reverse and module not in module_to_path:
            return set(), True
        for dependent in dependents_of(module, reverse):
            path = module_to_path.get(dependent, "")
            if is_test_file(path):
                selected.add(path)
    return selected, False


def is_test_file(rel_path: str) -> bool:
    """True only for real pytest test modules (not conftest/__init__/helpers)."""
    if not rel_path.startswith(f"{TEST_ROOT}/"):
        return False
    name = Path(rel_path).name
    return name.startswith("test_") or name.endswith("_test.py")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Git ref to diff against (default: origin/main).",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        help="Explicit changed files instead of a git diff.",
    )
    parser.add_argument(
        "--format",
        choices=("list", "pytest"),
        default="list",
        help="'list' prints one test file per line (or ALL); "
        "'pytest' prints ready-to-use pytest path args.",
    )
    parser.add_argument("--output", help="Write result to this file too.")
    args = parser.parse_args(argv)

    root = repo_root()
    module_to_path, forward = build_graph(root)
    reverse = invert(forward)

    files = args.files if args.files is not None else changed_files(args.base, root)
    selected, trigger_all = select(files, module_to_path, reverse)

    if trigger_all:
        lines = ["tests"] if args.format == "pytest" else ["ALL"]
    else:
        ordered = sorted(selected)
        if args.format == "pytest":
            lines = ordered if ordered else []
        else:
            lines = ordered if ordered else []

    text = "\n".join(lines)
    sys.stdout.write(text + ("\n" if text else ""))
    if args.output:
        Path(args.output).write_text(text + ("\n" if text else ""), encoding="utf-8")

    # Emit a short human summary on stderr (kept off stdout for clean piping).
    if trigger_all:
        sys.stderr.write(
            f"[test-impact] {len(files)} changed file(s) -> FULL SUITE "
            "(trigger-all file touched)\n"
        )
    else:
        sys.stderr.write(
            f"[test-impact] {len(files)} changed file(s) -> "
            f"{len(selected)} test file(s) selected\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
