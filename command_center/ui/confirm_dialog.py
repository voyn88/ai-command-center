"""Generic confirmation gate for destructive actions (`DESIGN_SYSTEM.md` §9.16,
`INTERACTION_MODEL.md` §11).

The friction an action gets should match its risk, not where in the app it
happens to live: reversible actions (dequeue, status move, send-to-review)
stay a single immediate click, while irreversible ones (task deletion, from
whichever surface triggers it) must clear an explicit checkbox inside a modal
dialog before the callback runs — and a bare click can never trigger it, since
the confirm button starts disabled *and* the checked state is re-verified
server-side (`AppTest.click()` can invoke a disabled button's handler
directly, same defense already used by `agent_launcher.py` and `aml_panel.py`).

One shared implementation instead of one inline `@st.dialog` per call site
keeps every destructive action on the same bar — first proven on task
deletion (`task_cards.py`), then reused unchanged in
`backlog_reconcile_panel.py`, so an accidental click deletes a task exactly
as easily as the previous, unguarded button — regardless of the panel it's
clicked from."""
from __future__ import annotations

from collections.abc import Callable

import streamlit as st


def open_confirmation(key_prefix: str) -> None:
    """Flag the dialog for `key_prefix` to open on the next render.

    Call this from the triggering button's `if st.button(...):` branch —
    never delete/mutate directly there.
    """
    st.session_state[f"{key_prefix}_confirm_open"] = True


def render_destructive_confirmation(
    *,
    key_prefix: str,
    dialog_title: str,
    warning: str,
    checkbox_label: str,
    confirm_label: str,
    on_confirm: Callable[[], None],
    confirm_icon: str = ":material/delete_forever:",
    cancel_label: str = "Отмена",
) -> None:
    """Render the confirm/cancel dialog for `key_prefix` if it was opened via
    `open_confirmation`. No-op otherwise.

    `on_confirm` runs only after the checkbox is rendered checked *and* that
    state is re-checked at submit time, so it is never reachable from a
    single click.
    """
    open_key = f"{key_prefix}_confirm_open"
    if not st.session_state.get(open_key):
        return

    @st.dialog(dialog_title)
    def _render() -> None:
        st.warning(warning)
        confirmed = st.checkbox(checkbox_label, key=f"{key_prefix}_confirmed")
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            clicked = st.button(
                confirm_label,
                type="primary",
                key=f"{key_prefix}_confirm_btn",
                disabled=not confirmed,
                icon=confirm_icon,
            )
        with cancel_col:
            if st.button(cancel_label, key=f"{key_prefix}_cancel_btn"):
                st.session_state[open_key] = False
                st.rerun()

        if not clicked:
            return
        # Defense in depth: AppTest and future callers can trigger a disabled
        # widget's handler programmatically, so never rely solely on the
        # button's disabled= state for a destructive action.
        if not confirmed:
            st.error("Подтвердите действие, установив флажок.")
            return

        on_confirm()
        st.session_state[open_key] = False
        st.rerun()

    _render()
