"""The web UI's label coverage test (web/src/lib/__tests__/labels.test.ts) walks
``web/src/lib/__fixtures__/backendValues.ts``. That list is hand-written, so
this test keeps it honest: it imports the REAL backend enums/constants (or
reads the SQL/source that defines them) and fails when the TypeScript fixture
is missing a value. The fixture is parsed as text; there is no build step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from command_center import models
from command_center.db import work_queue_read
from command_center.runtime import providers
from command_center.runtime.db.core import RUN_STATES
from command_center.webapi import serializers

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "web" / "src" / "lib" / "__fixtures__" / "backendValues.ts"
SQL_DIR = REPO_ROOT / "command_center" / "db" / "sql"


def fixture_list(name: str) -> set[str]:
    text = FIXTURE.read_text(encoding="utf-8")
    match = re.search(rf"export const {name}\s*=\s*\[(.*?)\]", text, re.S)
    assert match, f"{name} is missing from {FIXTURE.name}"
    # drop // and /* */ comments, then collect quoted strings
    body = re.sub(r"//[^\n]*|/\*.*?\*/", "", match.group(1), flags=re.S)
    return set(re.findall(r"'([^']*)'", body))


def missing(name: str, backend: set[str]) -> set[str]:
    return backend - fixture_list(name)


def test_run_states() -> None:
    assert not missing("RUN_STATES", set(RUN_STATES))


def test_queue_states() -> None:
    assert not missing("QUEUE_STATES", set(work_queue_read._STATES))


def test_attempt_states() -> None:
    sql = (SQL_DIR / "0002_queue_claim.up.sql").read_text(encoding="utf-8")
    match = re.search(r"work_attempt_state_valid\s*CHECK \(state IN \(([^)]*)\)", sql)
    assert match
    assert not missing("ATTEMPT_STATES", set(re.findall(r"'(\w+)'", match.group(1))))


def test_repository_states() -> None:
    backend = set(serializers._UNHEALTHY_REPOSITORY_STATES) | {"ok", "unconfigured"}
    assert not missing("REPOSITORY_STATES", backend)


def test_verdicts() -> None:
    assert not missing("VERDICTS", set(models.VERDICT_LABELS))


def test_kpi_meta_keys() -> None:
    source = (REPO_ROOT / "command_center" / "webapi" / "serializers.py").read_text(encoding="utf-8")
    assert not missing("KPI_META_KEYS", set(re.findall(r'"meta_key":\s*"(\w+)"', source)))


def test_reason_codes() -> None:
    backend = set(providers.PROVIDER_UNAVAILABLE_REASONS)
    for path in sorted(SQL_DIR.glob("*.up.sql")):
        sql = path.read_text(encoding="utf-8")
        backend |= set(re.findall(r"(?:dead_reason|outcome_reason)\s*=\s*'([a-z_]+)'", sql))
    assert not missing("REASON_CODES", backend)


def test_backlog_statuses() -> None:
    latest = None
    for path in sorted(SQL_DIR.glob("*.up.sql")):
        sql = path.read_text(encoding="utf-8")
        match = re.search(r"backlog_task_status_vocabulary CHECK \(status IN\s*\(([^)]*)\)", sql)
        if match:
            latest = set(re.findall(r"'([\w-]+)'", match.group(1)))
    assert latest
    assert not missing("BACKLOG_STATUSES", latest)


def test_helper_detects_a_missing_value() -> None:
    with pytest.raises(AssertionError):
        assert not missing("RUN_STATES", {"NOT_A_REAL_STATE"})
