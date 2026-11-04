from __future__ import annotations

import pytest

from command_center.worker import admission


def test_two_lanes_cannot_hold_mutation_admission(monkeypatch, tmp_path):
    monkeypatch.setenv('AICC_SINGLE_MUTATING_WRITER','1')
    monkeypatch.setenv('AICC_DATA_DIR',str(tmp_path))
    with admission.hold('AICC'):
        with pytest.raises(admission.AdmissionRefused) as error:
            with admission.hold('AICC'):
                pytest.fail('second writer admitted')
        assert error.value.contention
    with admission.hold('AICC'):
        pass


def test_pause_and_project_scope_prevent_writer(monkeypatch):
    monkeypatch.setenv('AICC_MUTATING_EXECUTION_PAUSED','1')
    with pytest.raises(admission.AdmissionRefused, match='paused'):
        with admission.hold('AICC'):
            pytest.fail('paused writer admitted')
    monkeypatch.delenv('AICC_MUTATING_EXECUTION_PAUSED')
    monkeypatch.setenv('AICC_MUTATING_PROJECT_ALLOWLIST','AICC')
    with pytest.raises(admission.AdmissionRefused, match='excluded'):
        with admission.hold('CRM'):
            pytest.fail('CRM writer admitted')


def test_exception_releases_mutation_admission(monkeypatch,tmp_path):
    monkeypatch.setenv('AICC_SINGLE_MUTATING_WRITER','1')
    monkeypatch.setenv('AICC_DATA_DIR',str(tmp_path))
    with pytest.raises(RuntimeError):
        with admission.hold('AICC'):
            raise RuntimeError('task failed')
    with admission.hold('AICC'):
        pass
