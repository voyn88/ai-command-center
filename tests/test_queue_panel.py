from command_center import execution_queue
from command_center.ui import queue_panel


def test_terminal_history_entries_include_only_terminal_states_and_newest_first():
    entries = [
        {"id": "w", "state": execution_queue.STATE_WAITING, "added_at": "2026-01-01T00:00:00Z"},
        {
            "id": "l_old",
            "state": execution_queue.STATE_LAUNCHED,
            "launched_at": "2026-01-01T00:00:01Z",
        },
        {
            "id": "c_new",
            "state": execution_queue.STATE_CANCELLED,
            "evaluated_at": "2026-01-01T00:00:03Z",
        },
        {
            "id": "l_new",
            "state": execution_queue.STATE_LAUNCHED,
            "launched_at": "2026-01-01T00:00:02Z",
        },
    ]

    history = queue_panel._terminal_history_entries(entries)

    assert [entry["id"] for entry in history] == ["c_new", "l_new", "l_old"]


def test_terminal_history_entries_applies_limit():
    entries = [
        {
            "id": f"l_{index}",
            "state": execution_queue.STATE_LAUNCHED,
            "launched_at": f"2026-01-01T00:00:{index:02d}Z",
        }
        for index in range(5)
    ]

    history = queue_panel._terminal_history_entries(entries, limit=2)

    assert [entry["id"] for entry in history] == ["l_4", "l_3"]
