"""Coverage for the Турнирный протокол Home-dashboard card:

1. ``command_center.ui.tournament_panel.champion_rows`` — pure row-shaping,
   no Streamlit call.
2. A full ``AppTest.from_file`` pass over the real Home dashboard
   (``app.py``, ``nav_page="dashboard"``) confirming the card renders and
   auto-publishes the current month's protocol on first view.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center import tournament, tournament_store
from command_center.ui import tournament_panel

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def test_champion_rows_lists_every_category_even_with_no_standings():
    protocol = {"month": "2026-08", "categories": {}}
    rows = tournament_panel.champion_rows(protocol)

    assert [row["name"] for row in rows] == list(tournament.CATEGORIES)
    assert all(row["right"] is None for row in rows)


def test_champion_rows_surfaces_the_category_leader():
    protocol = {
        "month": "2026-08",
        "categories": {
            "Dev": [
                {"participant": "claude", "completed": 3, "rank": 1},
                {"participant": "codex", "completed": 1, "rank": 2},
            ]
        },
    }
    rows = tournament_panel.champion_rows(protocol)
    dev_row = next(row for row in rows if row["name"] == "Dev")

    assert "claude" in dev_row["meta"]
    assert "3" in dev_row["meta"]
    assert dev_row["right"] == "2 участников"


def test_dashboard_page_renders_tournament_protocol_card():
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "dashboard"
    at.run()

    assert not at.exception
    body = " ".join(m.value for m in at.markdown)
    assert "Турнирный протокол" in body
    for category in tournament.CATEGORIES:
        assert category in body
    # Viewing the dashboard published the current month's protocol.
    assert tournament_store.get_protocol(tournament.current_month()) is not None
