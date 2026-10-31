"""Pin: the execution-attempt claim does not depend on the repository lease.

`VOYN-W0-AICC-SRV-04b-DEPENDENCY-FIRST` / ADR-0011. SRV-04-SCOPE separates
two entities that look alike:

* repository lease (AIOS platform, SRV-04a) — "who may mutate repository R"
* execution-attempt claim (AICC, SRV-04b) — "who is executing attempt N of W"

Before any compensation for platform-lease outages could be designed into the
claim path, the dependency itself had to be shown to exist. It does not: the
claim protocol decides exclusivity, redelivery and durable-ack from facts
local to the AICC database. The real lease dependency lives at the mutation
site (`worktree_lease` / `writer_lease`) and is already fail-closed there.

These tests are the mechanical half of that proof. They run offline — no
PostgreSQL, no `voyn-lease` binary — and fail if a later change wires the
platform lease into the claim surface.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLAIM_SQL = ROOT / "command_center/db/sql/0002_queue_claim.up.sql"
CLAIM_STORE = ROOT / "command_center/db/work_queue_store.py"
CLAIM_DAEMON = ROOT / "command_center/worker/daemon.py"
ADR = ROOT / "docs/adr/0011-claim-does-not-depend-on-repository-lease.md"

# Names that would constitute a claim→platform-lease dependency if they
# appeared in executable claim-protocol code (not in comments / ADR prose).
_PLATFORM_LEASE_TOKENS = (
    "repo_lease",
    "VOYN_LEASE",
    "voyn-lease",
    "voyn_lease",
    "worktree_lease",
    "writer_lease",
    "lease_client",
    "identity_assert",
)

_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _sql_without_comments(text: str) -> str:
    """Strip SQL comments so a mention in the 0002 header cannot satisfy a
    positive check, and cannot trip a negative one either."""
    return _SQL_LINE_COMMENT.sub("", _SQL_BLOCK_COMMENT.sub("", text))


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def test_adr_0011_records_the_no_dependency_conclusion() -> None:
    text = ADR.read_text(encoding="utf-8")
    assert "VOYN-W0-AICC-SRV-04b-DEPENDENCY-FIRST" in text
    assert "no dependency exists" in text.lower()
    assert "fail-closed" in text
    # Compensation is rejected for the claim path; the mutation site keeps it.
    assert "No compensation for AIOS/repo_lease" in text or (
        "no dependency to compensate" in text.lower()
    )


def test_claim_sql_has_no_executable_platform_lease_reference() -> None:
    """`0002_queue_claim.up.sql` may *name* repo_lease in its scope header;
    the executable body must not read it, join it, or call into it."""
    body = _sql_without_comments(CLAIM_SQL.read_text(encoding="utf-8"))
    offenders = [token for token in _PLATFORM_LEASE_TOKENS if token in body]
    assert offenders == [], (
        "0002's executable SQL must not reference the platform lease or its "
        f"AICC stand-ins; found {offenders}. Claim exclusivity is local to "
        "the AICC database (ADR-0011)."
    )


def test_work_queue_store_does_not_import_or_invoke_the_platform_lease() -> None:
    """The Python claim surface is a thin wrapper over 0002; a lease import
    here would be the exact cross-database node the dependency proof rules
    out."""
    source = CLAIM_STORE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CLAIM_STORE))
    imported = _imported_module_names(tree)

    lease_imports = {
        name
        for name in imported
        if any(
            token in name
            for token in (
                "worktree_lease",
                "writer_lease",
                "lease_client",
                "voyn_lease",
            )
        )
    }
    assert not lease_imports, lease_imports

    # Env / CLI tokens as string constants would be the other wiring path.
    string_constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    lease_strings = {
        value
        for value in string_constants
        if any(token in value for token in ("VOYN_LEASE", "voyn-lease", "repo_lease"))
    }
    assert not lease_strings, lease_strings

    # The only SQL the store may call is the local 0002 protocol
    # (claim/ownership plus the control-plane enqueue).
    sql_literals = sorted(
        value for value in string_constants if "SELECT" in value.upper()
    )
    assert sql_literals, "expected at least the queue_* call sites"
    for literal in sql_literals:
        assert re.search(
            r"\bqueue_(claim|heartbeat|complete|fail|enqueue)\b", literal
        ), (
            f"work_queue_store must only call the local queue_* protocol; "
            f"found {literal!r}"
        )


def test_daemon_claims_through_the_store_not_the_lease_tool() -> None:
    """The worker daemon is the production claim call site. It must reach
    `WorkQueueStore.claim` and must not import the platform-lease modules —
    those are for the mutation handler, after a claim already succeeded."""
    source = CLAIM_DAEMON.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CLAIM_DAEMON))
    imported = _imported_module_names(tree)

    assert any("work_queue_store" in name for name in imported), imported

    lease_imports = {
        name
        for name in imported
        if any(
            token in name
            for token in ("worktree_lease", "writer_lease", "lease_client")
        )
    }
    assert not lease_imports, (
        "daemon.py claims work; lease modules belong in the mutation handler "
        f"(handlers.py), not here. Found {lease_imports}."
    )


@pytest.mark.parametrize(
    "path",
    [CLAIM_SQL, CLAIM_STORE, CLAIM_DAEMON, ADR],
    ids=lambda p: p.name,
)
def test_pin_targets_exist(path: Path) -> None:
    assert path.is_file(), path
