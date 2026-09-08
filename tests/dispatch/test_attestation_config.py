"""Tests for `AttestationRecord` persistence (`dispatch.attestation_config`).

`AICC_DATA_DIR` is redirected to a temp dir by the session conftest, so these
writes never touch the developer's real `data/`.
"""

from __future__ import annotations

from pathlib import Path

from command_center.dispatch import attestation_config
from command_center.dispatch.attestation import AttestationRecord, CertificationCase

ROOT = Path("/unused-because-AICC_DATA_DIR-overrides")


def test_load_records_returns_empty_map_when_nothing_saved():
    assert attestation_config.load_records(ROOT) == {}


def test_save_then_load_roundtrips_a_record():
    record = AttestationRecord(
        agent_id="claude_code",
        test_results=(CertificationCase(name="ok_case", passed=True),),
        critical_incidents=0,
    )

    saved = attestation_config.save_record(ROOT, record, actor="tester")

    assert saved.recorded_by == "tester"
    assert saved.recorded_at is not None

    loaded = attestation_config.load_records(ROOT)
    assert set(loaded) == {"claude_code"}
    assert loaded["claude_code"].test_results == (
        CertificationCase(name="ok_case", passed=True),
    )
    assert loaded["claude_code"].recorded_by == "tester"


def test_save_record_upserts_without_disturbing_other_agents():
    attestation_config.save_record(
        ROOT, AttestationRecord(agent_id="claude_code", critical_incidents=0)
    )
    attestation_config.save_record(
        ROOT, AttestationRecord(agent_id="codex", critical_incidents=1)
    )

    loaded = attestation_config.load_records(ROOT)

    assert set(loaded) == {"claude_code", "codex"}
    assert loaded["codex"].critical_incidents == 1

    # Re-saving claude_code must not clobber codex's record.
    attestation_config.save_record(
        ROOT,
        AttestationRecord(
            agent_id="claude_code",
            test_results=(CertificationCase(name="ok_case", passed=True),),
        ),
    )
    loaded_again = attestation_config.load_records(ROOT)
    assert set(loaded_again) == {"claude_code", "codex"}
    assert loaded_again["codex"].critical_incidents == 1


def test_load_records_is_fail_closed_on_a_garbage_store():
    path = attestation_config.record_file_path(ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all", encoding="utf-8")

    assert attestation_config.load_records(ROOT) == {}


def test_load_records_drops_non_string_keys():
    from command_center import storage

    storage.atomic_write_json(
        attestation_config.record_file_path(ROOT),
        {"": {"critical_incidents": 0}, "claude_code": {"critical_incidents": 0}},
    )

    loaded = attestation_config.load_records(ROOT)
    assert set(loaded) == {"claude_code"}
