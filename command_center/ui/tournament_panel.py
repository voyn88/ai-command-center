"""Турнирный протокол — the Home-dashboard card for the monthly Субъектные
турниры standings.

Presentation only: it takes an already-published protocol dict (see
`command_center.tournament_store`) and paints it via the same
`home_dashboard` row primitives every other Home card uses. It never
publishes, computes or reads a protocol itself — that stays the caller's job,
so this module stays Streamlit-only and trivially testable with a fixed
fixture dict.
"""

from __future__ import annotations

from command_center import tournament
from command_center.ui import home_dashboard

_ICON = "\U0001f3c6"  # 🏆


def champion_rows(protocol: dict) -> list[dict]:
    """One `home_dashboard.simple_rows` row per category — the category's
    current champion, or a "no participants yet" placeholder in the same
    shape, so all five tournament categories are always visible even before
    any of them has a completed run this month."""
    categories = protocol.get("categories") or {}
    rows: list[dict] = []
    for category in tournament.CATEGORIES:
        standings = categories.get(category) or []
        if standings:
            leader = standings[0]
            meta = f"{leader['participant']} · побед в этом месяце: {leader['completed']}"
            right = f"{len(standings)} участников"
            accent = "amber"
        else:
            meta = "Нет завершённых прогонов в этой категории в этом месяце"
            right = None
            accent = "slate"
        rows.append(
            {
                "icon": _ICON,
                "name": category,
                "meta": meta,
                "right": right,
                "right_accent": accent,
            }
        )
    return rows


def render(protocol: dict) -> None:
    home_dashboard.card_open("Турнирный протокол", protocol.get("month"))
    home_dashboard.simple_rows(champion_rows(protocol))
    home_dashboard.card_close()
