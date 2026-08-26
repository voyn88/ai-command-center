"""The Home dashboard — the styled overview page from the approved design.

A single at-a-glance screen: welcome + KPI tiles, the execution queue with live
progress, project health, a Kanban overview, quick actions, and an AI-Supervisor
side panel (status, per-project health, recommendations, alerts, active agents).

Pure presentation over data the caller has already assembled — this module holds
the dark-dashboard CSS and the HTML/Streamlit layout, no business logic, no I/O.
Everything shown is real: counts come from the live task/run state, not the
mockup's placeholder deltas, so nothing here fabricates a "+3 from yesterday"
the system cannot actually measure.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

import streamlit as st

from command_center.ui import theme

# Accent palette from the design (indigo/blue/green/amber/teal/violet), kept in
# one place and reused so a tile, a progress bar and a status dot of the same
# meaning always share a colour.
_DARK_ACCENTS = {
    "indigo": "#a5b4fc",
    "blue": "#60a5fa",
    "green": "#4ade80",
    "amber": "#fbbf24",
    "red": "#f87171",
    "teal": "#5eead4",
    "violet": "#d8b4fe",
    "slate": "#cbd5e1",
}

_LIGHT_ACCENTS = {
    "indigo": "#4f46a5",
    "blue": "#1d4ed8",
    "green": "#15803d",
    "amber": "#92400e",
    "red": "#b91c1c",
    "teal": "#0f766e",
    "violet": "#7e22ce",
    "slate": "#475569",
}


def _pal(dark: bool) -> dict[str, str]:
    """Surface/border/text tokens for the card chrome. The design is dark; the
    light variant keeps the same structure on a bright surface so the page never
    looks broken if the viewer runs the light theme."""
    if dark:
        return {
            "bg": "#0b1020",
            "card": "#141b2e",
            "card2": "#1b2438",
            "border": "#26304a",
            "text": "#e6ebf5",
            "muted": "#8b96b0",
            "track": "#26304a",
        }
    return {
        "bg": "#f6f8fc",
        "card": "#ffffff",
        "card2": "#f2f5fb",
        "border": "#dfe5f0",
        "text": "#1f2430",
        "muted": "#5b6478",
        "track": "#e6ebf3",
    }


def inject_css() -> None:
    # Emit on every run. Streamlit drops any st.markdown a rerun does not
    # re-emit, so the old once-per-session guard left the entire landing
    # (KPI tiles, gauges, .hx-* cards) unstyled after the first interaction or
    # navigation (audit H6). inject_css is called once per render_home_dashboard,
    # so re-emitting here is one style block per render, not per widget.
    try:
        dark = theme.current_theme_type() == "dark"
    except Exception:  # noqa: BLE001
        dark = True
    p = _pal(dark)
    palette = _DARK_ACCENTS if dark else _LIGHT_ACCENTS
    accents = "\n".join(f"  --hx-{k}: {v};" for k, v in palette.items())
    st.markdown(
        f"""
<style>
:root {{
{accents}
  --hx-card: {p['card']};
  --hx-card2: {p['card2']};
  --hx-border: {p['border']};
  --hx-text: {p['text']};
  --hx-muted: {p['muted']};
  --hx-track: {p['track']};
}}
.hx-card {{
  background: var(--hx-card);
  border: 1px solid var(--hx-border);
  border-radius: 16px;
  padding: 18px 20px;
  margin-bottom: 16px;
}}
.hx-card-head {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; }}
.hx-card-title {{ font-size: 1.05rem; font-weight: 700; color: var(--hx-text); }}
.hx-card-link {{ font-size: 0.8rem; color: var(--hx-muted); }}
.hx-page-title {{ font-size:1.55rem; line-height:1.25; margin:0 0 0.25rem; color:var(--hx-text); }}

.hx-kpis {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap:16px; margin-bottom:16px; }}
.hx-kpi {{
  background: var(--hx-card); border:1px solid var(--hx-border); border-radius:16px;
  padding:16px 18px; position:relative; overflow:hidden; min-width:0;
}}
.hx-kpi::after {{ content:""; position:absolute; right:-30px; top:-30px; width:90px; height:90px;
  border-radius:50%; background: var(--hx-accent); opacity:0.12; }}
.hx-kpi .hx-kpi-label {{ font-size:0.82rem; color:var(--hx-muted); font-weight:600; }}
.hx-kpi .hx-kpi-value {{ font-size:2.2rem; font-weight:800; color:var(--hx-text); line-height:1.15;
  font-variant-numeric:tabular-nums; }}
.hx-kpi .hx-kpi-sub {{ font-size:0.78rem; color:var(--hx-muted); }}
.hx-kpi .hx-kpi-ic {{ position:absolute; right:16px; top:16px; width:38px; height:38px; border-radius:11px;
  display:flex; align-items:center; justify-content:center; font-size:1.1rem;
  background: color-mix(in srgb, var(--hx-accent) 22%, transparent); }}

.hx-row {{ display:flex; align-items:center; gap:12px; padding:10px 0; border-top:1px solid var(--hx-border); }}
.hx-row:first-child {{ border-top:none; }}
.hx-ic {{ width:34px; height:34px; border-radius:10px; display:flex; align-items:center; justify-content:center;
  background: var(--hx-card2); font-size:1rem; flex:none; }}
.hx-grow {{ flex:1; min-width:0; }}
.hx-name {{ font-weight:600; color:var(--hx-text); font-size:0.92rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.hx-meta {{ font-size:0.76rem; color:var(--hx-muted); }}
.hx-bar {{ height:7px; border-radius:6px; background:var(--hx-track); overflow:hidden; margin-top:6px; }}
.hx-bar > span {{ display:block; height:100%; border-radius:6px; background:var(--hx-accent); }}
.hx-pct {{ font-variant-numeric:tabular-nums; font-weight:700; color:var(--hx-text); font-size:0.85rem; width:44px; text-align:right; }}
.hx-status {{ font-size:0.78rem; font-weight:600; display:flex; align-items:center; gap:6px; white-space:nowrap; }}
.hx-dot {{ width:8px; height:8px; border-radius:50%; background:var(--hx-accent); display:inline-block; }}

.hx-kanban {{ display:grid; grid-template-columns: repeat(5, 1fr); gap:12px; }}
.hx-col {{ border:1px solid var(--hx-border); border-radius:12px; padding:14px 10px; text-align:center;
  background: color-mix(in srgb, var(--hx-accent) 10%, var(--hx-card)); }}
.hx-col .hx-col-name {{ font-size:0.78rem; color:var(--hx-muted); font-weight:600; }}
.hx-col .hx-col-num {{ font-size:1.7rem; font-weight:800; color:var(--hx-text); }}
.hx-col .hx-col-unit {{ font-size:0.7rem; color:var(--hx-muted); }}

.hx-health-num {{ font-size:2.6rem; font-weight:800; color:var(--hx-text); }}
.hx-metric {{ display:flex; align-items:center; justify-content:space-between; padding:6px 0; font-size:0.85rem; }}
.hx-metric .hx-metric-name {{ color:var(--hx-text); }}
.hx-metric .hx-metric-val {{ color:var(--hx-muted); font-variant-numeric:tabular-nums; font-weight:700; }}

.hx-foot {{ display:flex; gap:16px; font-size:0.8rem; color:var(--hx-muted); margin-top:12px; }}
.hx-chip {{ font-size:0.72rem; padding:2px 9px; border-radius:999px; font-weight:600;
  background: color-mix(in srgb, var(--hx-accent) 20%, transparent); color: var(--hx-accent); }}
button:focus-visible, a:focus-visible, [tabindex]:focus-visible {{
  outline: 3px solid var(--hx-blue) !important;
  outline-offset: 3px !important;
}}
[data-testid="stCaptionContainer"],
kbd[aria-label^="Shortcut"] {{
  opacity: 1 !important;
}}
[data-testid="stCaptionContainer"] p,
kbd[aria-label^="Shortcut"] {{
  color: var(--hx-muted) !important;
}}
a[aria-label="Link to heading"] {{
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
  min-width: 24px;
  min-height: 24px;
}}
@media (max-width: 420px) {{
  .hx-kpis, .hx-kanban {{ grid-template-columns: minmax(0, 1fr); }}
  .hx-row, .hx-foot {{ flex-wrap: wrap; }}
  .hx-status {{ white-space: normal; }}
}}
</style>
""",
        unsafe_allow_html=True,
    )


def _esc(x) -> str:
    return html.escape(str(x if x is not None else "—"))


# --------------------------------------------------------------------------
# Small render helpers (return HTML strings so cards compose cleanly)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Kpi:
    label: str
    value: int | str
    sub: str
    icon: str
    accent: str
    spark: tuple[int, ...] = ()  # small series for the sparkline; empty = none


def _sparkline(series: tuple[int, ...], accent: str) -> str:
    """A tiny inline-SVG line chart for a KPI tile — the design's sparkline,
    drawn from a real short series (no chart lib, CSP-safe)."""
    if len(series) < 2:
        return ""
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1
    w, h = 200, 34
    step = w / (len(series) - 1)
    pts = " ".join(
        f"{i * step:.1f},{h - 2 - (v - lo) / span * (h - 6):.1f}" for i, v in enumerate(series)
    )
    return (
        f"<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none' role='img' "
        f"aria-label='Тренд: {_esc(', '.join(str(item) for item in series))}' "
        f"style='width:100%; height:34px; margin-top:6px; display:block;'>"
        f"<polyline points='{pts}' fill='none' stroke='var(--hx-{accent})' stroke-width='2.5' "
        f"stroke-linecap='round' stroke-linejoin='round' opacity='0.9'/></svg>"
    )


def kpi_tiles(kpis: list[Kpi]) -> None:
    cells = "".join(
        f"<div class='hx-kpi' style='--hx-accent:var(--hx-{k.accent})'>"
        f"<div class='hx-kpi-ic' aria-hidden='true'>{k.icon}</div>"
        f"<div class='hx-kpi-label'>{_esc(k.label)}</div>"
        f"<div class='hx-kpi-value'>{_esc(k.value)}</div>"
        f"<div class='hx-kpi-sub'>{_esc(k.sub)}</div>"
        f"{_sparkline(k.spark, k.accent)}"
        f"</div>"
        for k in kpis
    )
    st.markdown(f"<div class='hx-kpis'>{cells}</div>", unsafe_allow_html=True)


def _bar(pct: int, accent: str, label: str = "Прогресс") -> str:
    pct = max(0, min(100, int(pct)))
    return (
        f"<div class='hx-bar' role='progressbar' aria-label='{_esc(label)}' "
        f"aria-valuemin='0' aria-valuemax='100' aria-valuenow='{pct}' "
        f"style='--hx-accent:var(--hx-{accent})'><span style='width:{pct}%'></span></div>"
    )


def card_open(title: str, link: str | None = None) -> None:
    link_html = f"<span class='hx-card-link'>{_esc(link)}</span>" if link else ""
    st.markdown(
        f"<div class='hx-card' role='region' aria-label='{_esc(title)}'>"
        f"<div class='hx-card-head'><h2 class='hx-card-title'>{_esc(title)}</h2>"
        f"{link_html}</div>",
        unsafe_allow_html=True,
    )


def card_close() -> None:
    st.markdown("</div>", unsafe_allow_html=True)


def queue_rows(rows: list[dict]) -> None:
    """rows: {icon, name, meta, pct, accent, status, status_accent}."""
    html_rows = []
    for r in rows:
        pct = r.get("pct")
        pct_html = (
            f"<div class='hx-pct'>{int(pct)}%</div>" if pct is not None else "<div class='hx-pct'>—</div>"
        )
        bar = _bar(pct, r.get("accent", "indigo"), f"Прогресс: {r.get('name') or 'запуск'}") if pct is not None else ""
        html_rows.append(
            f"<div class='hx-row'>"
            f"<div class='hx-ic' aria-hidden='true'>{r.get('icon','•')}</div>"
            f"<div class='hx-grow'><div class='hx-name'>{_esc(r.get('name'))}</div>"
            f"<div class='hx-meta'>{_esc(r.get('meta'))}</div>{bar}</div>"
            f"{pct_html}"
            f"<div class='hx-status' role='status' aria-label='Статус: {_esc(r.get('status'))}' "
            f"style='--hx-accent:var(--hx-{r.get('status_accent','slate')})'>"
            f"<span class='hx-dot' aria-hidden='true'></span>{_esc(r.get('status'))}</div>"
            f"</div>"
        )
    st.markdown("".join(html_rows), unsafe_allow_html=True)


def simple_rows(rows: list[dict]) -> None:
    """rows: {icon, name, meta, right, right_accent}."""
    html_rows = []
    for r in rows:
        right = r.get("right")
        right_html = (
            f"<div class='hx-status' role='status' aria-label='Статус: {_esc(right)}' "
            f"style='--hx-accent:var(--hx-{r.get('right_accent','slate')})'>"
            f"<span class='hx-dot' aria-hidden='true'></span>{_esc(right)}</div>"
            if right
            else ""
        )
        html_rows.append(
            f"<div class='hx-row'>"
            f"<div class='hx-ic' aria-hidden='true'>{r.get('icon','•')}</div>"
            f"<div class='hx-grow'><div class='hx-name'>{_esc(r.get('name'))}</div>"
            f"<div class='hx-meta'>{_esc(r.get('meta'))}</div></div>"
            f"{right_html}</div>"
        )
    st.markdown("".join(html_rows), unsafe_allow_html=True)


def queue_footer(
    total: int,
    running: int,
    completed: int,
    failed: int,
    *,
    loaded: int | None = None,
    window_limit: int = 200,
) -> None:
    loaded = total if loaded is None else loaded
    st.markdown(
        f"<div class='hx-foot' aria-label='Сводка запусков'>"
        f"<span>Всего запусков (runtime.db): <b style='color:var(--hx-text)'>{total}</b></span>"
        f"<span>Окно: последние {loaded} (лимит {window_limit})</span>"
        f"<span style='color:var(--hx-green)'>В работе в окне: {running}</span>"
        f"<span style='color:var(--hx-blue)'>Завершено в окне: {completed}</span>"
        f"<span style='color:var(--hx-red)'>Ошибки в окне: {failed}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


def kanban_overview(columns: list[tuple[str, int, str]]) -> None:
    """columns: (name, count, accent)."""
    cells = "".join(
        f"<div class='hx-col' style='--hx-accent:var(--hx-{acc})'>"
        f"<div class='hx-col-name'>{_esc(name)}</div>"
        f"<div class='hx-col-num'>{count}</div>"
        f"<div class='hx-col-unit'>задач</div></div>"
        for name, count, acc in columns
    )
    st.markdown(f"<div class='hx-kanban'>{cells}</div>", unsafe_allow_html=True)


def health_gauge(percent: int, label: str, accent: str = "teal") -> None:
    """An SVG donut gauge — the design's centrepiece, rendered inline (no
    external chart lib, CSP-safe)."""
    pct = max(0, min(100, int(percent)))
    r = 54
    circ = 2 * 3.14159 * r
    dash = circ * pct / 100
    st.markdown(
        f"""
<div style='display:flex; justify-content:center; margin:6px 0 4px 0;'>
<svg width='150' height='150' viewBox='0 0 150 150' role='img'
  aria-label='Здоровье проекта: {pct}%, {_esc(label)}'>
  <circle cx='75' cy='75' r='{r}' fill='none' stroke='var(--hx-track)' stroke-width='12'/>
  <circle cx='75' cy='75' r='{r}' fill='none' stroke='var(--hx-{accent})' stroke-width='12'
    stroke-linecap='round' stroke-dasharray='{dash:.1f} {circ:.1f}' transform='rotate(-90 75 75)'/>
  <text x='75' y='72' text-anchor='middle' font-size='30' font-weight='800' fill='var(--hx-text)'>{pct}%</text>
  <text x='75' y='94' text-anchor='middle' font-size='12' fill='var(--hx-muted)'>{_esc(label)}</text>
</svg></div>
""",
        unsafe_allow_html=True,
    )


def metric_list(metrics: list[tuple[str, int, str]]) -> None:
    """metrics: (name, percent, accent)."""
    rows = "".join(
        f"<div class='hx-metric'><span class='hx-metric-name'>"
        f"<span class='hx-dot' aria-hidden='true' style='--hx-accent:var(--hx-{acc}); background:var(--hx-{acc})'></span> {_esc(name)}</span>"
        f"<span class='hx-metric-val'>{pct}%</span></div>"
        for name, pct, acc in metrics
    )
    st.markdown(rows, unsafe_allow_html=True)


def supervisor_status(percent: int, label: str, *, status: str = "Active", accent: str = "green") -> None:
    st.markdown(
        f"<div class='hx-card'><div class='hx-card-head'>"
        f"<span class='hx-card-title'>AI Supervisor</span>"
        f"<span class='hx-status' role='status' aria-label='AI Supervisor: {_esc(status)}' "
        f"style='--hx-accent:var(--hx-{accent})'><span class='hx-dot' aria-hidden='true'></span>{_esc(status)}</span></div>"
        f"<div style='display:flex; justify-content:space-between; align-items:baseline;'>"
        f"<span class='hx-meta'>Успех терминальных запусков (7д, окно ≤200)</span>"
        f"<span class='hx-health-num' style='font-size:1.6rem'>{int(percent)}%</span></div>"
        f"{_bar(percent, accent, 'Здоровье AI Supervisor')}"
        f"<div class='hx-meta' style='margin-top:6px'>{_esc(label)}</div></div>",
        unsafe_allow_html=True,
    )


def delivery_rows(rows: list[dict], *, window_label: str) -> None:
    """Render canonical delivery/runtime truth without colour-only meaning."""
    body = []
    for row in rows:
        body.append(
            f"<div class='hx-row' role='{_esc(row.get('semantic_role') or 'status')}' "
            f"aria-label='{_esc(row.get('label'))}: {_esc(row.get('description'))}'>"
            f"<div class='hx-ic' aria-hidden='true'>◆</div>"
            f"<div class='hx-grow'><div class='hx-name'>{_esc(row.get('label'))}</div>"
            f"<div class='hx-meta'>{_esc(row.get('description'))}</div>"
            f"<div class='hx-meta'>Run {_esc(str(row.get('run_id') or 'unknown')[:12])}</div></div>"
            f"<div class='hx-status' style='--hx-accent:var(--hx-{_esc(row.get('accent') or 'slate')})'>"
            f"<span class='hx-dot' aria-hidden='true'></span>{_esc(row.get('state'))}</div></div>"
        )
    st.markdown(
        f"<div aria-label='{_esc(window_label)}'>{''.join(body)}</div>",
        unsafe_allow_html=True,
    )
