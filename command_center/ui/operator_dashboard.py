"""Operator dashboard — the tokens-styled command surface (W1-DASHBOARD-UI).

The desktop dashboard built on the Wave-0/1 foundation: it renders entirely
from the ``/api/v1`` read/write surface (via
:class:`command_center.ui.dashboard_client.DashboardClient`) and is styled
*only* through the canonical design-token package (``command_center/design``:
``tokens.css`` + ``primitives.css``). No raw hex lives here — every colour is a
``var(--token)`` resolved from those files, so both themes (dark-first + light)
stay in step with the mobile app and the rest of the shell.

Presentation only. This module holds no business logic and no data access of
its own (the ``command_center.ui`` boundary): it takes a client, asks it for
each endpoint's typed response, and paints the result. Every async section
carries the full VOYN-W0-F3 state contract — idle / loading / error / empty /
success — so nothing ever hangs as an ambiguous blank.

Sections and their wiring:

* KPI row, «Активность агентов», «Проекты», «Требуют внимания»
  -> ``GET /api/v1/dashboard``
* «Находки Советника» (cards with «→ в задачу»)
  -> ``GET /api/v1/advisor/proposals`` + ``POST /proposals/{id}/promote``
* «Дайджест» (night feed) -> ``GET /api/v1/digest``
* «Мой день» -> ``GET /api/v1/owner-items``
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import streamlit as st

from command_center import design
from command_center.ui import dashboard_client, theme

# --------------------------------------------------------------------------
# Design-token stylesheet — read the canonical package, never re-declare colour
# --------------------------------------------------------------------------

_DESIGN_DIR = Path(design.__file__).resolve().parent

# Body of ``:root[data-theme="<t>"] { ... }`` inside the generated tokens.css.
# Extracting a slice of the *generated* file (whose hex lives in the allowed
# source) lets us re-emit the active theme as a plain ``:root`` rule so the
# shell's chosen theme wins over the OS ``prefers-color-scheme`` media query —
# without this module ever authoring a colour literal.
_THEME_BLOCK_RE = r":root\[data-theme=\"{theme}\"\]\s*\{{(?P<body>[^}}]*)\}}"


def _root_override(tokens_css: str, theme_type: str) -> str:
    """Return a ``:root {{ ... }}`` block forcing ``theme_type``'s palette.

    Falls back to an empty string (tokens.css keeps its dark default + light
    media query) if the theme is unknown or the block cannot be located, so a
    parsing miss degrades to "follow the OS", never to a broken page.
    """
    if theme_type not in ("dark", "light"):
        return ""
    match = re.search(_THEME_BLOCK_RE.format(theme=theme_type), tokens_css)
    if match is None:
        return ""
    return f":root {{{match.group('body')}}}"


def tokens_style(theme_type: str) -> str:
    """The full ``<style>`` payload: tokens + primitives + active-theme override.

    Read fresh on every call so an edit to the design package shows up on the
    next rerun, and so the emitted block always matches the live theme.
    """
    tokens_css = (_DESIGN_DIR / "tokens.css").read_text(encoding="utf-8")
    primitives_css = (_DESIGN_DIR / "primitives.css").read_text(encoding="utf-8")
    override = _root_override(tokens_css, theme_type)
    # Layout-only scaffolding for this surface. Colour, spacing, radius, type —
    # all via var(--token); no literal here is a colour.
    layout_css = """
.ocd-head { display:flex; align-items:baseline; justify-content:space-between;
  gap:var(--space-4); flex-wrap:wrap; margin-bottom:var(--space-5); }
.ocd-title { font-family:var(--font-sans); font-size:var(--fs-2xl);
  font-weight:var(--fw-semibold); letter-spacing:var(--tracking-tight);
  color:var(--text); line-height:1.1; }
.ocd-sub { font-family:var(--font-sans); font-size:var(--fs-md);
  color:var(--text-3); }
.ocd-kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:var(--space-4); margin-bottom:var(--space-5); }
.ocd-sec { font-family:var(--font-sans); font-size:var(--fs-sm);
  font-weight:var(--fw-semibold); letter-spacing:var(--tracking-caps);
  text-transform:uppercase; color:var(--text-3); margin:var(--space-5) 0 var(--space-2); }
.ocd-item { margin-bottom:var(--space-3); }
.ocd-item__title { font-family:var(--font-sans); font-size:var(--fs-md);
  font-weight:var(--fw-semibold); color:var(--text); }
.ocd-item__body { font-family:var(--font-sans); font-size:var(--fs-base);
  color:var(--text-2); margin-top:var(--space-1); }
.ocd-item__meta { display:flex; flex-wrap:wrap; gap:var(--space-2);
  align-items:center; margin-top:var(--space-2); }
.ocd-note { font-family:var(--font-sans); font-size:var(--fs-base);
  color:var(--text-3); padding:var(--space-3) 0; }
.ocd-note--err { color:var(--crit); }
.ocd-bar-wrap { display:flex; align-items:center; gap:var(--space-3);
  margin-top:var(--space-2); }
.ocd-pct { font-family:var(--font-mono); font-size:var(--fs-sm);
  color:var(--text-3); min-width:38px; text-align:right; }
.ocd-skeleton { height:14px; border-radius:var(--radius-pill);
  background:color-mix(in srgb, var(--text-3) 18%, transparent);
  margin:var(--space-2) 0; }
.ocd-skeleton--wide { width:70%; }
.ocd-skeleton--mid { width:45%; }
@media (max-width:640px) {
  .ocd-kpis { grid-template-columns:minmax(0,1fr); }
  .ocd-head { flex-direction:column; align-items:flex-start; }
}
"""
    return f"<style>\n{tokens_css}\n{primitives_css}\n{override}\n{layout_css}</style>"


def inject_css(theme_type: str | None = None) -> None:
    """Emit the token/primitive stylesheet.

    Re-emitted on every render (not guarded once-per-session): Streamlit drops
    any ``st.markdown`` a rerun does not re-emit, which would leave the surface
    unstyled after the first interaction (the audit-H6 lesson from
    ``home_dashboard.inject_css``).
    """
    if theme_type is None:
        try:
            theme_type = theme.current_theme_type()
        except Exception:  # noqa: BLE001 - context unavailable outside a run
            theme_type = "dark"
    st.markdown(tokens_style(theme_type), unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Async section state — the idle/loading/error/empty/success contract
# --------------------------------------------------------------------------

IDLE = "idle"
LOADING = "loading"
ERROR = "error"
EMPTY = "empty"
SUCCESS = "success"


@dataclass(frozen=True)
class SectionState:
    """One async section's resolved state plus its payload.

    ``status`` is exactly one of the five contract states; ``data`` carries the
    typed endpoint response on ``success``/``empty`` and ``error`` carries the
    reason on ``error``. Section renderers switch on ``status`` alone, so every
    section handles all five states the same way.
    """

    status: str
    data: Any = None
    error: str | None = None


def load_section(
    fetch: Callable[[], Any], is_empty: Callable[[Any], bool]
) -> SectionState:
    """Run one endpoint call and classify it into a :class:`SectionState`.

    Any exception the client raises (a real HTTP client would raise on a
    transport or 5xx error just the same) becomes the ``error`` state rather
    than propagating — one failing section never blanks the whole page.
    """
    try:
        payload = fetch()
    except Exception as exc:  # noqa: BLE001 - surfaced as the error state
        return SectionState(status=ERROR, error=str(exc) or exc.__class__.__name__)
    if is_empty(payload):
        return SectionState(status=EMPTY, data=payload)
    return SectionState(status=SUCCESS, data=payload)


# --------------------------------------------------------------------------
# Small HTML helpers (return strings; all user text escaped)
# --------------------------------------------------------------------------


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def _chip(label: str, variant: str = "") -> str:
    cls = "chip" + (f" chip--{variant}" if variant else "")
    return f"<span class='{cls}'>{_esc(label)}</span>"


def _card(inner: str, *, raised: bool = False) -> str:
    cls = "card ocd-item" + (" card--raised" if raised else "")
    return f"<div class='{cls}'>{inner}</div>"


def _bar(pct: int) -> str:
    pct = max(0, min(100, int(pct)))
    variant = "ok" if pct >= 80 else "warn" if pct >= 40 else "crit"
    return (
        f"<div class='ocd-bar-wrap'><div class='bar' role='progressbar' "
        f"aria-valuemin='0' aria-valuemax='100' aria-valuenow='{pct}'>"
        f"<div class='bar__fill bar__fill--{variant}' style='width:{pct}%'></div></div>"
        f"<span class='ocd-pct'>{pct}%</span></div>"
    )


def _section_title(text: str) -> None:
    st.markdown(f"<div class='ocd-sec'>{_esc(text)}</div>", unsafe_allow_html=True)


def _skeleton() -> None:
    st.markdown(
        "<div class='card' aria-busy='true' aria-label='Загрузка'>"
        "<div class='ocd-skeleton ocd-skeleton--wide'></div>"
        "<div class='ocd-skeleton ocd-skeleton--mid'></div></div>",
        unsafe_allow_html=True,
    )


def _note(text: str, *, error: bool = False) -> None:
    cls = "ocd-note ocd-note--err" if error else "ocd-note"
    role = "alert" if error else "status"
    st.markdown(
        f"<div class='{cls}' role='{role}'>{_esc(text)}</div>",
        unsafe_allow_html=True,
    )


def _idle_note(text: str = "Ожидание данных…") -> None:
    st.markdown(
        f"<div class='ocd-note' role='status'>{_esc(text)}</div>",
        unsafe_allow_html=True,
    )


def _retry(key: str) -> None:
    """A real retry control — a rerun re-runs every section's fetch."""
    if st.button("Повторить", key=f"ocd_retry_{key}", type="secondary"):
        st.rerun()


def _navigate(page: str) -> None:
    """Stage a cross-page navigation the way ``app.py`` expects (pending_nav)."""
    st.session_state["pending_nav"] = page
    st.rerun()


# --------------------------------------------------------------------------
# Section renderers — each takes a SectionState and handles all five states
# --------------------------------------------------------------------------

_KIND_CHIP = {
    "trend": "cyan",
    "ux": "violet",
    "optimization": "info",
    "competitor": "warn",
    "feedback": "ok",
}


def render_kpis(state: SectionState) -> None:
    if state.status == LOADING:
        _skeleton()
        return
    if state.status == IDLE:
        _idle_note()
        return
    if state.status == ERROR:
        _note(f"Не удалось загрузить сводку: {state.error}", error=True)
        _retry("kpis")
        return
    dash = state.data
    agents = dash.agents
    cells = [
        ("Живые агенты", agents.running, "info"),
        ("В очереди", agents.queued, "warn"),
        ("Требуют внимания", agents.attention, "crit" if agents.attention else ""),
        ("Задачи", dash.task_counts.total, ""),
        ("Проекты", len(dash.projects), ""),
    ]
    tiles = "".join(
        f"<div class='card'><div class='kpi'>"
        f"<div class='kpi__value'>{_esc(value)}</div>"
        f"<div class='kpi__label'>{_esc(label)}</div>"
        f"{_chip('внимание', variant) if variant == 'crit' else ''}"
        f"</div></div>"
        for label, value, variant in cells
    )
    st.markdown(f"<div class='ocd-kpis'>{tiles}</div>", unsafe_allow_html=True)


def render_activity(state: SectionState) -> None:
    _section_title("Активность агентов")
    if state.status == LOADING:
        _skeleton()
        return
    if state.status == IDLE:
        _idle_note()
        return
    if state.status == ERROR:
        _note(f"Активность недоступна: {state.error}", error=True)
        _retry("activity")
        return
    items = state.data.activity
    if not items:
        _note("Нет активных прогонов")
        return
    for item in items:
        variant = "info" if item.kind == "run" else "violet"
        kind_label = "прогон" if item.kind == "run" else "коммит"
        proj_chip = _chip(item.project, "") if item.project else ""
        meta = " · ".join(_esc(part) for part in (item.project, item.ts) if part)
        meta_html = f"<div class='ocd-item__body'>{meta}</div>" if meta else ""
        st.markdown(
            _card(
                f"<div class='ocd-item__meta'>{_chip(kind_label, variant)}{proj_chip}</div>"
                f"<div class='ocd-item__title'>{_esc(item.title)}</div>"
                f"{meta_html}"
            ),
            unsafe_allow_html=True,
        )


def render_projects(state: SectionState) -> None:
    _section_title("Проекты")
    if state.status == LOADING:
        _skeleton()
        return
    if state.status == IDLE:
        _idle_note()
        return
    if state.status == ERROR:
        _note(f"Проекты недоступны: {state.error}", error=True)
        _retry("projects")
        return
    projects = state.data.projects
    if not projects:
        _note("Нет активных проектов")
        return
    for proj in projects:
        chips = [_chip(proj.kind or "проект", "")]
        if proj.redacted:
            chips.append(_chip("скрыто", "violet"))
        else:
            chips.append(
                _chip("здоров", "ok") if proj.healthy else _chip("внимание", "crit")
            )
        branch = ""
        if proj.health and proj.health.branch:
            branch = f"<div class='ocd-item__body'>{_esc(proj.health.branch)}</div>"
        bar = _bar(proj.progress) if proj.progress is not None else ""
        st.markdown(
            _card(
                f"<div class='ocd-item__title'>{_esc(proj.name)}</div>"
                f"<div class='ocd-item__meta'>{''.join(chips)}</div>"
                f"{branch}{bar}"
            ),
            unsafe_allow_html=True,
        )
        if st.button(
            "Открыть проект",
            key=f"ocd_project_{proj.id}",
            type="secondary",
        ):
            st.session_state["project_browser_select"] = proj.project_ref
            _navigate("projects")


def render_advisor(
    state: SectionState, *, on_promote: Callable[[str], None]
) -> None:
    _section_title("Находки Советника")
    if state.status == LOADING:
        _skeleton()
        return
    if state.status == IDLE:
        _idle_note()
        return
    if state.status == ERROR:
        _note(f"Советник недоступен: {state.error}", error=True)
        _retry("advisor")
        return
    proposals = state.data.proposals
    if not proposals:
        _note("Пока нет находок — Советник ничего не предлагает")
        return
    for prop in proposals:
        kind_variant = _KIND_CHIP.get(prop.kind, "")
        meta_chips = [_chip(prop.kind, kind_variant)]
        if prop.expected_gain:
            meta_chips.append(_chip(f"выгода: {prop.expected_gain}", "ok"))
        if prop.effort:
            meta_chips.append(_chip(f"усилия: {prop.effort}", "warn"))
        body = (
            f"<div class='ocd-item__body'>{_esc(prop.body)}</div>"
            if prop.body
            else ""
        )
        st.markdown(
            _card(
                f"<div class='ocd-item__title'>{_esc(prop.title)}</div>"
                f"{body}"
                f"<div class='ocd-item__meta'>{''.join(meta_chips)}</div>",
                raised=True,
            ),
            unsafe_allow_html=True,
        )
        if prop.status in ("new", "accepted"):
            if st.button(
                "→ в задачу",
                key=f"ocd_promote_{prop.id}",
                type="primary",
            ):
                on_promote(prop.id)
        else:
            label = "в задаче" if prop.status == "converted" else "отклонено"
            st.markdown(
                f"<div class='ocd-item__meta'>{_chip(label, 'info')}</div>",
                unsafe_allow_html=True,
            )


def render_digest(state: SectionState) -> None:
    _section_title("Дайджест")
    if state.status == LOADING:
        _skeleton()
        return
    if state.status == IDLE:
        _idle_note()
        return
    if state.status == ERROR:
        _note(f"Дайджест недоступен: {state.error}", error=True)
        _retry("digest")
        return
    items = state.data.items
    if not items:
        _note("Дайджест пуст")
        return
    for item in items:
        cat = _chip(item.category, "cyan") if item.category else ""
        refs = "".join(_chip(ref, "") for ref in item.refs)
        body = (
            f"<div class='ocd-item__body'>{_esc(item.body)}</div>"
            if item.body
            else ""
        )
        meta = (
            f"<div class='ocd-item__meta'>{cat}{refs}</div>" if (cat or refs) else ""
        )
        st.markdown(
            _card(
                f"<div class='ocd-item__title'>{_esc(item.title)}</div>{body}{meta}"
            ),
            unsafe_allow_html=True,
        )


def render_attention(state: SectionState) -> None:
    _section_title("Требуют внимания")
    if state.status == LOADING:
        _skeleton()
        return
    if state.status == IDLE:
        _idle_note()
        return
    if state.status == ERROR:
        _note(f"Список внимания недоступен: {state.error}", error=True)
        _retry("attention")
        return
    items = state.data.attention
    if not items:
        _note("Ничего не требует внимания")
        return
    for index, item in enumerate(items):
        variant = "crit" if item.kind == "run" else "warn"
        kind_label = "прогон" if item.kind == "run" else "задача"
        detail = (
            f"<div class='ocd-item__body'>{_esc(item.detail)}</div>"
            if item.detail
            else ""
        )
        proj = _chip(item.project, "") if item.project else ""
        st.markdown(
            _card(
                f"<div class='ocd-item__meta'>{_chip(kind_label, variant)}{proj}</div>"
                f"<div class='ocd-item__title'>{_esc(item.title)}</div>{detail}"
            ),
            unsafe_allow_html=True,
        )
        target = "runs" if item.kind == "run" else "kanban"
        if st.button(
            "Открыть",
            key=f"ocd_attention_{index}_{item.kind}",
            type="secondary",
        ):
            _navigate(target)


def render_owner_day(state: SectionState) -> None:
    _section_title("Мой день")
    if state.status == LOADING:
        _skeleton()
        return
    if state.status == IDLE:
        _idle_note()
        return
    if state.status == ERROR:
        _note(f"«Мой день» недоступен: {state.error}", error=True)
        _retry("owner")
        return
    items = state.data.items
    if not items:
        _note("На сегодня задач владельца нет")
        return
    for item in items:
        status_chip = (
            _chip("готово", "ok") if item.done else _chip("в работе", "warn")
        )
        due = _chip(f"срок: {item.due}", "info") if item.due else ""
        detail = (
            f"<div class='ocd-item__body'>{_esc(item.detail)}</div>"
            if item.detail
            else ""
        )
        st.markdown(
            _card(
                f"<div class='ocd-item__title'>{_esc(item.title)}</div>{detail}"
                f"<div class='ocd-item__meta'>{status_chip}{due}</div>"
            ),
            unsafe_allow_html=True,
        )


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def _greeting(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if hour < 6:
        return "Доброй ночи"
    if hour < 12:
        return "Доброе утро"
    if hour < 18:
        return "Добрый день"
    return "Добрый вечер"


@dataclass
class _Sections:
    """The resolved state of every section, so the header can summarise and the
    body can paint without re-fetching."""

    kpis: SectionState
    activity: SectionState
    projects: SectionState
    advisor: SectionState
    digest: SectionState
    attention: SectionState
    owner: SectionState
    errors: list[str] = field(default_factory=list)


def _load_all(client: dashboard_client.DashboardClient) -> _Sections:
    """Fetch every endpoint once under a spinner, classifying each into its
    state. The single dashboard response feeds four visual sections."""
    with st.spinner("Загрузка панели…"):
        dash_state = load_section(client.dashboard, lambda _d: False)
        advisor_state = load_section(
            client.advisor_proposals, lambda page: not page.proposals
        )
        digest_state = load_section(
            client.digest, lambda page: not page.items
        )
        owner_state = load_section(
            client.owner_items, lambda page: not page.items
        )

    # The dashboard payload drives KPIs, activity, projects and attention; each
    # inherits the fetch's error state and is judged empty independently.
    def _derive(is_empty: Callable[[Any], bool]) -> SectionState:
        if dash_state.status in (ERROR, LOADING, IDLE):
            return dash_state
        return (
            SectionState(status=EMPTY, data=dash_state.data)
            if is_empty(dash_state.data)
            else dash_state
        )

    sections = _Sections(
        kpis=dash_state,
        activity=_derive(lambda d: not d.activity),
        projects=_derive(lambda d: not d.projects),
        attention=_derive(lambda d: not d.attention),
        advisor=advisor_state,
        digest=digest_state,
        owner=owner_state,
    )
    sections.errors = [
        state.error
        for state in (dash_state, advisor_state, digest_state, owner_state)
        if state.status == ERROR and state.error
    ]
    return sections


def _promote_handler(client: dashboard_client.DashboardClient) -> Callable[[str], None]:
    def _promote(proposal_id: str) -> None:
        try:
            result = client.promote_proposal(proposal_id)
        except Exception as exc:  # noqa: BLE001 - a 409/terminal status, surfaced
            st.toast(f"Не удалось создать задачу: {exc}", icon="⚠️")
            return
        if result is None:
            st.toast("Находка не найдена или скрыта", icon="⚠️")
            return
        st.toast(f"Создана задача {result.task_id}", icon="✅")
        st.rerun()

    return _promote


def render(client: dashboard_client.DashboardClient | None = None) -> None:
    """Render the whole operator dashboard.

    ``client`` defaults to the in-process API client; the UI tests inject a
    fake (or a raising stub) to exercise the empty/error states from mocked
    responses.
    """
    if client is None:
        client = dashboard_client.InProcessDashboardClient()

    inject_css()

    sections = _load_all(client)

    subtitle = (
        "Все данные — из /api/v1."
        if not sections.errors
        else f"Часть источников недоступна ({len(sections.errors)})."
    )
    st.markdown(
        f"<div class='ocd-head'>"
        f"<div class='ocd-title'>{_esc(_greeting())}</div>"
        f"<div class='ocd-sub'>{_esc(subtitle)}</div></div>",
        unsafe_allow_html=True,
    )

    render_kpis(sections.kpis)

    main, rail = st.columns([2, 1], gap="large")
    with main:
        render_activity(sections.activity)
        render_projects(sections.projects)
        render_digest(sections.digest)
    with rail:
        render_advisor(sections.advisor, on_promote=_promote_handler(client))
        render_attention(sections.attention)
        render_owner_day(sections.owner)
