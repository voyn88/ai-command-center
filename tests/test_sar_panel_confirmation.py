"""Confirmation-gate coverage for `command_center.ui.sar_panel`.

Filing a SAR with the regulator (`submit_to_regulator`) is the one
irreversible, legally significant action on this panel — everything else
(review, approve, reopen-to-draft) stays reversible and one-click. It must
clear the same shared `confirm_dialog` gate as task deletion
(`task_cards.py`) and backlog-reconciliation deletion
(`backlog_reconcile_panel.py`), per `INTERACTION_MODEL.md` §11: friction
scales with risk, not with which panel happens to trigger the action.
"""
from __future__ import annotations

from streamlit.testing.v1 import AppTest

from command_center import sar_store


def _panel_script() -> None:
    # Re-exec'd as a standalone script by AppTest, so it must be self-contained:
    # the db path comes from AICC_DATA_DIR (the isolated_data_dir fixture),
    # never a captured closure variable.
    import os
    from pathlib import Path

    from command_center import sar_store
    from command_center.ui import sar_panel

    db_path = Path(os.environ["AICC_DATA_DIR"]) / "aml_sars.db"
    sar_panel.render(sar_db=db_path)


def _run() -> AppTest:
    return AppTest.from_function(_panel_script, default_timeout=30).run()


def _approved_sar(root):
    db_path = root / "aml_sars.db"
    sar_store.init_db(db_path)
    sar = sar_store.create_sar(
        db_path,
        sar_type="str",
        narrative="Structuring across three accounts.",
        created_by="ComplianceOfficer",
    )
    sar_store.submit_for_review(db_path, sar["id"], actor="ComplianceOfficer")
    sar_store.approve_sar(db_path, sar["id"], actor="ComplianceOfficer")
    return db_path, sar_store.get_sar(db_path, sar["id"])


def test_submit_to_regulator_requires_explicit_confirmation(isolated_data_dir):
    root = isolated_data_dir
    db_path, sar = _approved_sar(root)

    at = _run()
    at.text_input(key="detail_ref").set_value(sar["sar_number"]).run()

    sub_key = f"sr_{sar['id']}"
    at.text_input(key=sub_key).set_value("RFM-2026-001").run()

    submit_button = at.button(key=f"sub_{sar['id']}")
    at = submit_button.click().run()

    # A single click must only open the dialog — the SAR must still be
    # "approved", not "submitted", exactly like task deletion isn't
    # triggered by one click on "Удалить".
    reloaded = sar_store.get_sar(db_path, sar["id"])
    assert reloaded["state"] == "approved"

    confirm_key_prefix = f"submit_{sar['id']}"
    confirm_button = at.button(key=f"{confirm_key_prefix}_confirm_btn")
    assert confirm_button.disabled is True

    at = at.checkbox(key=f"{confirm_key_prefix}_confirmed").check().run()
    at = at.button(key=f"{confirm_key_prefix}_confirm_btn").click().run()

    reloaded = sar_store.get_sar(db_path, sar["id"])
    assert reloaded["state"] == "submitted"
    assert reloaded["submission_ref"] == "RFM-2026-001"
