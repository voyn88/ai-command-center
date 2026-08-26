"""Pre-flight checks for a mirror slice — the ones acceptance kept having to run.

Eight slices produced five rejections, and four of them were caught by two
mechanical probes the writer could have run before pushing:

* **statements** — the SQL each mirror generates, diffed against a base SHA.
  Green test suites cannot distinguish a changed `ON CONFLICT` target, a
  reordered parameter list or a lost `ORDER BY` from correct behaviour; on
  small fixtures a wrong statement produces the right rows. Slice 7's
  acceptance found this by building the probe itself. A diff is only a diff,
  though: a table the base does not have is compared against nothing, so those
  are printed in full instead of silently counted as fine.
* **counts** — how many tests actually need PostgreSQL. Two slices shipped a
  transposed figure in a commit message, which is a false claim in a document
  that goes into history.

Run before `git push`, paste the output into the PR. The point is not
discipline; it is that a check written down is a check that survives the end of
a long slice, when discipline is what runs out first.

Usage
-----
    uv run python scripts/mirror_slice_checks.py statements --base origin/main
    uv run python scripts/mirror_slice_checks.py counts tests/db/test_batch_stores.py
    uv run python scripts/mirror_slice_checks.py all --base origin/main tests/db/...

`statements` needs no database: it records what would be sent.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

def _root() -> Path:
    """The repository this run measures — never guessed.

    An earlier version preferred the parent of `scripts/` and fell back to the
    working directory. That silently defeated the whole tool: probing an older
    SHA copies *this* script into a throwaway worktree, so `parents[1]` pointed
    back at the current checkout and the "base" run measured the head tree.
    Every table then compared IDENTICAL to itself, including four tables that
    do not exist at the base at all — a green result that meant nothing.

    So the base run is told where to look with `--root`, and there is no
    inference left to be wrong about.
    """
    for index, argument in enumerate(sys.argv):
        if argument == "--root" and index + 1 < len(sys.argv):
            return Path(sys.argv[index + 1]).resolve()
    return Path(__file__).resolve().parents[1]


ROOT = _root()


# --- recording connection ----------------------------------------------------


class _Cursor:
    def __init__(self, log: list[dict]) -> None:
        self._log = log

    def execute(self, sql: str, params: object = None) -> None:
        self._log.append({"sql": " ".join(sql.split()), "params": _renderable(params)})

    def fetchone(self):  # noqa: ANN201 - shaped for the callers below
        """Answer by what was asked, not by how many times.

        The first version replied on call parity — a sequence name on odd
        calls, an integer on even ones. That happens to fit `resync_identity`
        today and would silently mis-answer the moment a mirror issues any
        other read, which is exactly the sort of quiet wrongness this tool is
        supposed to expose in *other* code.
        """
        sql = self._log[-1]["sql"] if self._log else ""
        if "pg_get_serial_sequence" in sql:
            return (f"public.{'unknown'}_id_seq",)
        return (1,)

    def fetchall(self):  # noqa: ANN201
        return []

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Connection:
    def __init__(self, log: list[dict]) -> None:
        self._log = log

    def cursor(self) -> _Cursor:
        return _Cursor(self._log)

    @contextmanager
    def transaction(self):  # noqa: ANN201
        yield self

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _renderable(params: object) -> object:
    """Parameters as JSON-safe values, with types kept visible.

    The type matters: a `datetime` reaching the driver where a string used to
    is precisely the kind of change this probe exists to catch, and rendering
    both as their `str()` would hide it.
    """
    if params is None:
        return None
    return [f"{type(value).__name__}:{value!r}" for value in params]


# --- probing every declared mirror -------------------------------------------


def _sample(spec: object) -> dict:
    row: dict[str, object] = {}
    for column in spec.columns:  # type: ignore[attr-defined]
        if column in spec.codec.json_values:  # type: ignore[attr-defined]
            row[column] = '{"b": 1, "a": 2}'
        elif column in spec.codec.timestamps:  # type: ignore[attr-defined]
            row[column] = "2026-08-14T00:00:00"
        elif column in spec.codec.flags:  # type: ignore[attr-defined]
            row[column] = 1
        else:
            row[column] = f"<{column}>"
    return row


def collect_statements() -> dict[str, list[dict]]:
    """Every statement every declared mirror would send, keyed by table."""
    sys.path.insert(0, str(ROOT))
    from command_center.db.table_mirror import MirroredTable, PostgresTableMirror

    import command_center.db as db_package

    out: dict[str, list[dict]] = {}
    for module_info in pkgutil.iter_modules(db_package.__path__):
        if not module_info.name.endswith("_store"):
            continue
        module = importlib.import_module(f"command_center.db.{module_info.name}")
        for attribute in vars(module).values():
            # `isinstance(spec, object)` was the original guard and is always
            # true — it read like a check and was not one. The real question is
            # whether the class declares a table.
            if (
                isinstance(attribute, type)
                and issubclass(attribute, PostgresTableMirror)
                and attribute is not PostgresTableMirror
                and isinstance(getattr(attribute, "spec", None), MirroredTable)
            ):
                log: list[dict] = []
                mirror = attribute(connection_factory=lambda log=log: _Connection(log))
                spec = attribute.spec
                mirror.upsert(_sample(spec))
                mirror.list_records()
                if spec.identity:
                    mirror.resync_identity()
                # Whole-predicate deletes exist on one table today; probe the
                # capability wherever a store exposes it rather than naming it.
                for extra in ("delete_day",):
                    if hasattr(mirror, extra):
                        getattr(mirror, extra)("2026-08-14")
                if spec.table in out:
                    raise SystemExit(
                        f"two mirrors declare `{spec.table}`: {attribute.__name__} and an "
                        "earlier one. A table with two mirrors has two opinions about its "
                        "statements, and this tool would have reported only the last."
                    )
                out[spec.table] = log
    return dict(sorted(out.items()))


def _collect_at(ref: str) -> dict[str, list[dict]]:
    """The same probe, run in a throwaway worktree at `ref`."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(tree), ref],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        try:
            probe = tree / "scripts" / "mirror_slice_checks.py"
            if not probe.exists():
                # The tool did not exist at `ref`; run this copy against that tree.
                probe = Path(__file__)
            result = subprocess.run(
                [sys.executable, str(probe), "statements", "--emit-json", "--root", str(tree)],
                cwd=tree,
                capture_output=True,
                text=True,
                check=False,
                env={"PYTHONPATH": str(tree), "PATH": "/usr/bin:/bin"},
            )
            if result.returncode != 0:
                raise SystemExit(f"probe failed at {ref}:\n{result.stderr}")
            return json.loads(result.stdout)
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )


def cmd_statements(args: argparse.Namespace) -> int:
    current = collect_statements()
    if args.emit_json:
        print(json.dumps(current))
        return 0
    if not args.base:
        for table, log in current.items():
            print(f"{table}: {len(log)} statements")
        return 0

    base = _collect_at(args.base)
    added = sorted(set(current) - set(base))
    removed = sorted(set(base) - set(current))
    changed = [t for t in sorted(set(current) & set(base)) if current[t] != base[t]]

    for table in sorted(set(current) & set(base)):
        print(f"{table:22s} {'IDENTICAL' if current[table] == base[table] else 'CHANGED'}")
    for table in added:
        print(f"{table:22s} NEW (not present at {args.base})")
    for table in removed:
        print(f"{table:22s} GONE (present at {args.base})")

    if changed:
        print(f"\n{len(changed)} table(s) generate different SQL than at {args.base}:\n")
        for table in changed:
            for before, after in zip(base[table], current[table], strict=False):
                if before != after:
                    print(f"  {table}\n    - {before['sql']}\n      {before['params']}")
                    print(f"    + {after['sql']}\n      {after['params']}\n")

    if added:
        # A diff has nothing to say about a table the base does not have, and
        # that is not a small corner: when a slice introduces the machinery as
        # well as its tables, *every* table is new and the comparison covers
        # nothing while still printing a reassuring list. Independent review
        # demonstrated it — a corrupted statement builder produced zero CHANGED
        # lines. So new tables get their SQL printed in full: unreviewable by
        # diff, reviewable by reading.
        print(f"\n{len(added)} new table(s) have no base to diff against — read their SQL:\n")
        for table in added:
            for entry in current[table]:
                print(f"  {table}\n    {entry['sql']}\n      {entry['params']}")
            print()
    # New tables set the exit code too. They were reported and then ignored by
    # it, so the exact scenario this printing exists for — a slice whose tables
    # are all new — still exited 0, and a `&& git push` would have run. A
    # non-zero exit here means "read this", not "something is broken".
    return 1 if (changed or added) else 0


# --- test counts -------------------------------------------------------------


def cmd_counts(args: argparse.Namespace) -> int:
    """How many of a file's tests need PostgreSQL, measured rather than counted."""
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", *args.paths, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    total = sum(1 for line in collected.stdout.splitlines() if "::" in line)

    # Copy the real environment and remove the one variable under test, rather
    # than constructing a minimal one: a hand-built env drops whatever the
    # interpreter needs on this machine (SYSTEMROOT on Windows, VIRTUAL_ENV
    # here) and the resulting failure looks like a test result.
    env_without_dsn = {k: v for k, v in os.environ.items() if k != "AICC_TEST_PG_ADMIN_DSN"}
    serverless = subprocess.run(
        [sys.executable, "-m", "pytest", *args.paths, "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env_without_dsn,
    )
    tail = serverless.stdout.strip().splitlines()[-1] if serverless.stdout.strip() else ""
    print(f"collected: {total}")
    print(f"without a database: {tail}")
    print(
        "→ the PG-backed count is the 'skipped' number and the rest need no server —"
        " true only while the PostgreSQL fixtures are the only thing that skips."
    )
    if "error" in tail.lower() or not tail:
        print("!! the serverless run did not report a summary; the split above is unusable")
        return 1
    return 0



# --- perturbation sweep --------------------------------------------------------


def _pytest_command(suite: str) -> list[str]:
    """`uv` when it is there, this interpreter when it is not — see
    `scripts/evidence.py` for why the hard-coded form was a defect."""
    if shutil.which("uv"):
        # `pytest` resolved alongside the extra, for the reason `evidence.py`
        # documents: without it uv takes pytest from PATH and the extra never
        # reaches the interpreter. This file pointed at that explanation while
        # keeping the broken form — review noticed the mismatch between the
        # comment and the command.
        return [
            "uv", "run",
            "--with", "psycopg-pool>=3.2,<4",
            "--with", "pytest",
            "pytest", suite, "-q",
        ]
    return [sys.executable, "-m", "pytest", suite, "-q"]


def cmd_sweep(args: argparse.Namespace) -> int:
    """Drop one mirror hook at a time and ask whether anything notices.

    This is the check that found what no suite had: three slices in a row
    shipped mirrors and dual-write hooks with **no test that ran a hook**, and
    the sweep measured it rather than suspecting it — 0 of 5, 0 of 4 and 0 of
    11 hooks noticed. It also found a mirror written by nothing at all, which
    the sweep alone cannot see (there is no hook to remove) and which the
    contract now checks separately.

    The question is asked of the whole suite, not of the slice's own test file.
    The first version asked the narrower question and reported a hook as
    uncovered when the covering test simply lived in another family's module —
    a false alarm sends the writer hunting a defect that is not there, which
    costs as much as missing one.

    A call site that cannot be commented out on a single line is reported as
    NOT PERTURBED rather than skipped. An unmeasured site must not read as a
    measured one.
    """
    target = Path(args.module)
    if not target.is_absolute():
        target = ROOT / target
    original = target.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    sites = [
        (number, line)
        for number, line in enumerate(lines, start=1)
        if re.match(r"^\s*_mirror\w*\(", line)
    ]
    if not sites:
        print(f"{args.module}: no `_mirror...(` call sites found")
        return 1

    # A green baseline first, and the sweep refuses to run without one.
    # Without it `caught = returncode != 0` cannot tell "the perturbation was
    # noticed" from "the suite was already red", so a broken suite reports
    # every hook as covered — independent acceptance produced `4/4 caught`
    # from a suite that touches none of this code. A probe that cannot fail is
    # the defect this whole file exists to find, in the file itself.
    baseline = subprocess.run(
        _pytest_command(args.suite), cwd=ROOT, capture_output=True, text=True
    )
    if baseline.returncode != 0:
        tail = [line for line in baseline.stdout.splitlines() if line.startswith("FAILED")]
        print(f"{args.suite} is already failing — a sweep against a red suite proves nothing:")
        for line in tail[:5]:
            print(f"  {line[:140]}")
        return 1

    print(f"{args.module}: {len(sites)} hook call sites, suite {args.suite} (baseline green)")
    unnoticed: list[str] = []
    try:
        for number, line in sites:
            if not line.rstrip().endswith(")"):
                print(f"  {number:>5}  NOT PERTURBED (multi-line call): {line.strip()}")
                continue
            patched = list(lines)
            indent = len(line) - len(line.lstrip())
            patched[number - 1] = " " * indent + "pass  # perturbed by the sweep\n"
            target.write_text("".join(patched), encoding="utf-8")
            result = subprocess.run(
                _pytest_command(args.suite), cwd=ROOT, capture_output=True, text=True
            )
            caught = result.returncode != 0
            named = [entry for entry in result.stdout.splitlines() if entry.startswith("FAILED")]
            print(f"  {number:>5}  {'caught' if caught else 'UNNOTICED'}: {line.strip()}")
            if caught and named:
                print(f"         {named[0][:140]}")
            if not caught:
                unnoticed.append(f"{args.module}:{number} {line.strip()}")
    finally:
        # Restored even on Ctrl-C: a probe that can leave the tree perturbed is
        # a probe that will one day be blamed for a real failure.
        target.write_text(original, encoding="utf-8")

    print(f"{len(sites) - len(unnoticed)}/{len(sites)} caught")
    for entry in unnoticed:
        print(f"  UNNOTICED {entry}")
    return 1 if unnoticed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    statements = sub.add_parser("statements", help="diff generated SQL against a base ref")
    statements.add_argument("--base", help="git ref to compare against (e.g. origin/main)")
    statements.add_argument("--emit-json", action="store_true", help=argparse.SUPPRESS)
    statements.add_argument("--root", help="repository to measure (used when probing a base SHA)")
    statements.set_defaults(func=cmd_statements)

    counts = sub.add_parser("counts", help="PG-backed vs serverless test split")
    counts.add_argument("paths", nargs="+")
    counts.set_defaults(func=cmd_counts)

    sweep = sub.add_parser("sweep", help="drop each mirror hook and see what notices")
    sweep.add_argument("module", help="authority module, e.g. command_center/runtime/db/proposal.py")
    sweep.add_argument("--suite", default="tests/db", help="suite to re-run per perturbation")
    sweep.set_defaults(func=cmd_sweep)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
