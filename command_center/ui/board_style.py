"""Visual layer for the Live Execution Center board — the colour and shape the
built-in widgets do not give for free.

Streamlit's native theme (`.streamlit/config.toml`) sets the app's base
palette, but `st.metric`/`st.container` render one flat neutral card regardless
of what the number *means*: a running count and a failure count look identical.
This module adds the semantic colour on top — tinted stat tiles and a coloured
accent rail per card — keyed to the *same* GitHub-derived palette the config
already defines, so nothing here invents a colour the rest of the app does not
already use.

It is the one place in the app that injects scoped CSS. That is a deliberate,
narrow exception to the "native theming only" rule the theme module documents:
`st.metric` genuinely cannot express "this tile is a danger tile", and the
alternative — four differently-worded captions — is exactly the grey wall this
redesign set out to remove. The CSS is scoped under `.aicc-board`, reads its
colours from variables injected per active theme (no `prefers-color-scheme`
guessing — the exact theme is read back via `theme.current_theme_type()`), and
touches nothing outside this board.
"""

from __future__ import annotations

import html

import streamlit as st

from command_center.design import Theme, color
from command_center.ui import live_board, theme

# Board palette, derived from the platform-canonical
# `command_center/design/tokens.json` (the same source `.streamlit/config.toml`'s
# `[theme.light]`/`[theme.dark]` mirror). Each bucket tone carries a *line* colour
# (the strong hue used for text, numbers and the rail) and a *fill* colour (a
# faint tint used for the tile background). The fills are the line hue at low
# alpha (see `_fill`) so they sit correctly on either background without a second
# hand-picked value — and, crucially, without a raw hex living here: change a hue
# once in `tokens.json` and the board follows.
def _board_palette(t: Theme) -> dict[str, str]:
    return {
        "surface": color("surface", t),
        "border": color("line", t),
        "muted": color("text-2", t),
        # Primary body text — used for the tile number and the section title so
        # they always clear WCAG AA. The state hue that used to colour them fails
        # 1.4.3 on its own faint tint (e.g. the light `ok` green is 2.52:1 on the
        # done-tile fill); the colour cue now rides the rail, dot, icon and label
        # instead, none of which carry text.
        "strong": color("text", t),
        "live_line": color("accent", t),
        "waiting_line": color("warn", t),
        "attention_line": color("crit", t),
        "done_line": color("ok", t),
    }


_PALETTE: dict[str, dict[str, str]] = {
    "light": _board_palette("light"),
    "dark": _board_palette("dark"),
}

# Per-bucket tone: which line colour a bucket's tile and cards wear.
_BUCKET_TONE: dict[str, str] = {
    live_board.BUCKET_LIVE: "live",
    live_board.BUCKET_WAITING: "waiting",
    live_board.BUCKET_ATTENTION: "attention",
    live_board.BUCKET_DONE: "done",
}

_BUCKET_ICON: dict[str, str] = {
    live_board.BUCKET_LIVE: "▶",
    live_board.BUCKET_WAITING: "⏳",
    live_board.BUCKET_ATTENTION: "⚠",
    live_board.BUCKET_DONE: "✓",
}

_STYLE_FLAG = "_aicc_board_style_injected"


def _fill(line_hex: str, *, dark: bool) -> str:
    """A faint tint of `line_hex` for a tile background: the same hue, low
    alpha, so one value works on both themes without a second swatch."""
    line_hex = line_hex.lstrip("#")
    r, g, b = (int(line_hex[i : i + 2], 16) for i in (0, 2, 4))
    alpha = 0.22 if dark else 0.10
    return f"rgba({r}, {g}, {b}, {alpha})"


def inject_once() -> None:
    """Emit the board's scoped stylesheet, once per script run.

    Streamlit re-runs the whole script on every interaction and fragment
    refresh; injecting the `<style>` each time is harmless but noisy in the DOM,
    so a session-scoped flag keeps it to one block. The flag is cleared at the
    top of the board render (`begin`) because a *fresh* full rerun starts a new
    session-state pass — see `begin`."""
    if st.session_state.get(_STYLE_FLAG):
        return
    st.session_state[_STYLE_FLAG] = True

    # `st.context.theme` is unavailable in some headless/AppTest passes; a
    # missing theme is not a reason to render an unstyled board, so fall back
    # to light rather than letting the lookup raise.
    try:
        theme_type = "dark" if theme.current_theme_type() == "dark" else "light"
    except Exception:  # noqa: BLE001 — theme context absent; light is a safe default
        theme_type = "light"
    pal = _PALETTE[theme_type]
    dark = theme_type == "dark"

    vars_ = "\n".join(
        f"  --aicc-{name.replace('_', '-')}: {value};"
        for name, value in pal.items()
    )
    fills = "\n".join(
        f"  --aicc-{tone}-fill: {_fill(pal[f'{tone}_line'], dark=dark)};"
        for tone in ("live", "waiting", "attention", "done")
    )

    # Variables live on `:root`, not on the `.aicc-board` wrapper: Streamlit
    # renders every `st.markdown` into its own container, so the wrapper is a
    # sibling of the tiles rather than their ancestor and scoped variables would
    # never reach them. `:root` resolves everywhere regardless of that
    # fragmentation; the `aicc-` prefix keeps the names from colliding with
    # Streamlit's own.
    st.markdown(
        f"""
<style>
:root {{
{vars_}
{fills}
}}
/* Responsive by construction: `auto-fit` + `minmax` lets the four tiles sit in
   one row on a wide screen and reflow to 2×2 (or a single column) as the page
   narrows or zooms in, instead of four tiles crushed past legibility or spilling
   past the edge. `clamp()` on the number keeps it bold but never so large it
   overflows a shrunk tile. This is the whole answer to "the design must not
   break when the page is scaled down". */
.aicc-tiles {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin: 2px 0 6px 0;
}}
.aicc-tile {{
  border: 1px solid var(--aicc-border);
  border-left: 4px solid var(--aicc-tone);
  border-radius: 10px;
  padding: 12px 14px;
  background: var(--aicc-tile-fill);
  min-width: 0;               /* let the tile shrink inside the grid track */
  overflow: hidden;
}}
.aicc-tile .aicc-tile-label {{
  font-size: clamp(0.72rem, 1.6vw, 0.80rem);
  font-weight: 600;
  color: var(--aicc-muted);
  letter-spacing: 0.01em;
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.aicc-tile .aicc-tile-value {{
  font-size: clamp(1.5rem, 4.5vw, 2.1rem);
  font-weight: 700;
  line-height: 1.1;
  color: var(--aicc-strong);
  font-variant-numeric: tabular-nums;
}}
.aicc-tile.aicc-zero .aicc-tile-value {{
  color: var(--aicc-muted);
  opacity: 0.55;
}}
.aicc-rail {{
  height: 3px;
  border-radius: 3px;
  background: var(--aicc-tone);
  margin: -2px 0 8px 0;
}}
.aicc-section-head {{
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 10px 0 2px 0;
}}
.aicc-section-head .aicc-dot {{
  width: 9px; height: 9px; border-radius: 50%;
  background: var(--aicc-tone);
  display: inline-block;
}}
.aicc-section-head .aicc-title {{
  font-size: clamp(0.95rem, 2.5vw, 1.05rem); font-weight: 700; color: var(--aicc-strong);
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
.aicc-section-head .aicc-count {{
  font-size: 0.85rem; color: var(--aicc-muted); font-variant-numeric: tabular-nums;
  flex: none;
}}
/* Buttons across the board read as one row: a single height, labels centred.
   `nowrap` keeps a control on one line, but the label may ellipsize rather than
   overflow its column when the page is narrowed/zoomed — the button never
   spills past its track. Streamlit gives every button a [data-testid="stButton"]
   wrapper; scoping to that keeps this off widgets that are not buttons. */
[data-testid="stButton"] > button {{
  min-height: 38px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
[data-testid="stButton"] > button > div,
[data-testid="stButton"] > button p {{
  justify-content: center;
  overflow: hidden;
  text-overflow: ellipsis;
}}
/* Wide content inside the board — long prompts, code, paths — scrolls within its
   own box instead of forcing the page to scroll sideways when scaled down. */
.aicc-board [data-testid="stCode"],
.aicc-board pre {{
  max-width: 100%;
  overflow-x: auto;
}}
</style>
""",
        unsafe_allow_html=True,
    )


def begin() -> None:
    """Open the board's style scope for this render.

    Called once at the very top of the board body: it re-arms `inject_once`
    (so a genuine full rerun re-emits the stylesheet exactly once) and opens the
    `.aicc-board` wrapper the CSS is scoped under. Streamlit closes the wrapper
    div itself at the end of the script pass, so there is no `end()` to forget.
    """
    st.session_state[_STYLE_FLAG] = False
    inject_once()
    st.markdown("<div class='aicc-board'>", unsafe_allow_html=True)


def stat_tiles(
    board: dict[str, list[dict]], *, counts: dict[str, int] | None = None
) -> None:
    """The four state counts as tinted, accented tiles instead of flat metrics.

    A zero tile is muted rather than coloured — an empty "attention" count is
    good news and must not read as a red alert."""
    order = (
        live_board.BUCKET_LIVE,
        live_board.BUCKET_WAITING,
        live_board.BUCKET_ATTENTION,
        live_board.BUCKET_DONE,
    )
    cells = []
    for bucket in order:
        tone = _BUCKET_TONE[bucket]
        count = counts[bucket] if counts is not None else len(board[bucket])
        zero = " aicc-zero" if count == 0 else ""
        label = html.escape(live_board.BUCKET_TITLES[bucket])
        cells.append(
            f"<div class='aicc-tile{zero}' style='--aicc-tone: var(--aicc-{tone}-line);"
            f" --aicc-tile-fill: var(--aicc-{tone}-fill);'>"
            f"<div class='aicc-tile-label'>{_BUCKET_ICON[bucket]} {label}</div>"
            f"<div class='aicc-tile-value'>{count}</div>"
            f"</div>"
        )
    st.markdown(f"<div class='aicc-tiles'>{''.join(cells)}</div>", unsafe_allow_html=True)


def section_head(bucket: str, count: int) -> None:
    """A coloured section header: a state-tinted dot, the title in the state
    colour, and the count muted beside it."""
    tone = _BUCKET_TONE.get(bucket, "live")
    title = html.escape(live_board.BUCKET_TITLES[bucket])
    st.markdown(
        f"<div class='aicc-section-head' style='--aicc-tone: var(--aicc-{tone}-line);'>"
        f"<span class='aicc-dot'></span>"
        f"<span class='aicc-title'>{title}</span>"
        f"<span class='aicc-count'>{count}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


def card_rail(bucket: str) -> None:
    """A thin coloured rule at the top of a card, carrying the card's state
    colour. Rendered as the first element inside the card container so a run's
    state is legible before a single word is read."""
    tone = _BUCKET_TONE.get(bucket, "live")
    st.markdown(
        f"<div class='aicc-rail' style='--aicc-tone: var(--aicc-{tone}-line);'></div>",
        unsafe_allow_html=True,
    )
