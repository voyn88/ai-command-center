"""Master Backlog page — ACC's read-only window onto the VOYN Backlog Engine.

The Backlog Engine owns the `Task` entity; ACC is a *reader* of its master store
(engine plan invariant #5: local models are only projections). This page renders
the live projection from `command_center.backlog_client`: where the store is and
how fresh it is, the totals and wave/priority/status/domain breakdown, the
executable queue derived from approved records, and a searchable/filterable table
of every record.

"How fresh" is read from the file's own render stamp when it has one (BO-S4:
`backlog-export` writes the projection every 5 minutes and stamps each render),
falling back to `mtime` for a hand-authored file. See `_render_freshness` for why
the two are not interchangeable.

It is emphatically read-only. There are no create/edit/delete widgets here — a
banner and per-surface captions say so, and the page never imports
`tasks_repository`, so browsing the master backlog can never touch ACC's local
`data/tasks.json`. Local task creation (the Create page, Kanban) is untouched by
this slice. The store's location is configuration (`AICC_MASTER_BACKLOG`); when it
is unset or missing, the page explains how to connect it instead of erroring.
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from command_center import backlog_client

# Sentinel for the "no filter" choice in each facet selectbox.
_ALL = "Все"


def _format_freshness(mtime: float | None) -> str:
    if mtime is None:
        return "—"
    stamp = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone()
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _format_local(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _render_freshness(column, projection: backlog_client.Projection) -> None:
    """The freshness metric, read from the file's own render stamp when it has
    one and from ``mtime`` when it does not.

    Which clock this shows is not cosmetic. ``mtime`` answers "when did *this
    host* last write these bytes", which any ``cp``/``scp``/checkout/container
    build resets to now — so a projection whose export tick died a week ago
    reads as seconds fresh as soon as it is copied anywhere, which is the
    silent staleness BO-S4 exists to end (the console booted 2026-09-03 on a
    file that stopped being true 2026-08-20). The stamp in the header answers
    "when was the store actually read", travels with the text, and is therefore
    what an owner is shown whenever it exists. A hand-authored backlog carries
    no stamp and no tick, so it keeps the mtime reading — there is no cadence
    for it to be late against — and the label says which of the two is on
    screen rather than letting them look alike.
    """
    stamp = projection.stamp
    if stamp is None:
        column.metric(
            "Актуальность (mtime)", _format_freshness(projection.source_mtime)
        )
        column.caption(
            "Файл без штампа рендера — вероятно, авторский. Показано время "
            "изменения файла; оно сбрасывается при любом копировании."
        )
        return

    column.metric("Актуальность (рендер)", _format_local(stamp.rendered_at))
    if stamp.is_stale(datetime.now(timezone.utc)):
        column.error(
            ":material/error: Проекция устарела: тик экспорта "
            "(`aicc-backlog-export.timer`, каждые 5 минут) молчит дольше "
            f"{int(backlog_client.PROJECTION_STALE_AFTER.total_seconds() // 60)} "
            "мин. Всё ниже — снимок на момент штампа, а не состояние стора."
        )
    else:
        column.caption(
            "Штамп в самом файле, не mtime. Записей в сторе на момент "
            f"рендера: {stamp.row_count}. Тик экспорта жив."
        )


def _facet(label: str, counts: dict[str, int], key: str) -> str | None:
    """A selectbox over a count dict; returns the chosen value or None for all."""
    options = [_ALL, *counts.keys()]
    chosen = st.selectbox(
        label,
        options,
        key=key,
        format_func=lambda value: (
            "Все" if value == _ALL else f"{value} ({counts.get(value, 0)})"
        ),
    )
    return None if chosen == _ALL else chosen


def render_master_backlog_page(path: str | None = None) -> None:
    """Render the read-only Master Backlog page from the live projection."""
    st.title(":material/inventory: Master Backlog")
    st.caption(
        "Единый источник задач принадлежит Backlog Engine. Это **read-only** "
        "проекция мастер-стора — ACC только читает, второго хранилища нет."
    )

    projection = backlog_client.load_projection(path)

    if not projection.exists:
        st.warning(
            "Мастер-бэклог не подключён. Укажите путь к файлу "
            f"`VOYN_TASKS_BACKLOG.md` в переменной окружения "
            f"`{backlog_client.MASTER_BACKLOG_ENV}` — ACC прочитает его как "
            "read-only проекцию, не создавая второго хранилища."
        )
        if projection.source_path is not None:
            st.caption(f"Ожидался файл: `{projection.source_path}` (не найден).")
        return

    st.info(
        ":material/lock: Только чтение · источник истины — **Backlog Engine (master)**. "
        "Изменения задач идут через Backlog API, а не здесь.",
        icon=None,
    )

    summary = backlog_client.summarize(projection)
    queue = backlog_client.execution_queue(projection)

    # --- Source / freshness -------------------------------------------------
    src_col, fresh_col = st.columns(2)
    src_col.metric("Источник", "master store")
    src_col.caption(f"`{projection.source_path}`")
    _render_freshness(fresh_col, projection)

    # --- Totals -------------------------------------------------------------
    m_total, m_approved, m_queue, m_errors = st.columns(4)
    m_total.metric("Всего записей", summary.total)
    m_approved.metric("Approved", summary.approved)
    m_queue.metric("В очереди исполнения", len(queue))
    m_errors.metric("Ошибок парсинга", summary.errors)

    # A generated file holds exactly as many record lines as its header says it
    # was rendered from, so a mismatch means lines were added or removed after
    # the render — i.e. someone edited the projection instead of the backlog.
    # Those edits are erased by the next tick and would otherwise leave no
    # trace at all (ADR-0011 files this as convention-only); the counts make
    # the line-count half of it visible.
    if backlog_client.stamp_matches_content(projection) is False:
        st.warning(
            ":material/edit_off: Файл изменён после рендера — число записей не "
            f"совпадает со штампом: в заголовке {projection.stamp.row_count}, "
            f"в файле {summary.total + summary.errors}. Это **проекция**: правки "
            "здесь исчезнут со следующим тиком экспорта, менять надо стор."
        )

    if summary.errors:
        with st.expander(f":material/warning: Непрочитанные строки ({summary.errors})"):
            for err in projection.errors:
                st.text(f"стр. {err.line_no}: {err.reason}")

    # --- Breakdown counts ---------------------------------------------------
    b_wave, b_priority, b_status, b_domain = st.columns(4)
    b_wave.markdown("**По волнам**")
    b_wave.table(_as_rows(summary.by_wave))
    b_priority.markdown("**По приоритету**")
    b_priority.table(_as_rows(summary.by_priority))
    b_status.markdown("**По статусу**")
    b_status.table(_as_rows(summary.by_status))
    b_domain.markdown("**По домену**")
    b_domain.table(_as_rows(summary.by_domain))

    # --- Execution queue ----------------------------------------------------
    st.subheader(":material/bolt: Очередь исполнения (approved)")
    st.caption(
        "Порядок, в котором Backlog Engine выдаёт задачи исполнителям: "
        "по приоритету, затем по волне. Claim/lease происходит в Backlog API."
    )
    st.dataframe(
        [
            {
                "id": rec.issue_id,
                "приоритет": rec.priority,
                "волна": rec.proposed_wave,
                "задача": rec.title,
                "домен": rec.parallel_domain,
            }
            for rec in queue[:50]
        ],
        width="stretch",
        hide_index=True,
    )

    # --- Searchable / filterable rows --------------------------------------
    st.subheader(":material/table_rows: Все записи")
    query = st.text_input(
        "Поиск",
        key="mb_query",
        placeholder="id, задача, owner, scope, acceptance…",
    )
    f_wave, f_priority, f_status, f_domain = st.columns(4)
    with f_wave:
        wave = _facet("Волна", summary.by_wave, "mb_wave")
    with f_priority:
        priority = _facet("Приоритет", summary.by_priority, "mb_priority")
    with f_status:
        status = _facet("Статус", summary.by_status, "mb_status")
    with f_domain:
        domain = _facet("Домен", summary.by_domain, "mb_domain")

    rows = backlog_client.filter_records(
        projection.records,
        query=query,
        wave=wave,
        priority=priority,
        status=status,
        domain=domain,
    )
    st.caption(f"Показано {len(rows)} из {summary.total} (read-only).")
    st.dataframe(
        [backlog_client.to_read_model(rec) for rec in rows],
        width="stretch",
        hide_index=True,
    )


def _as_rows(counts: dict[str, int]) -> list[dict]:
    return [{"": key, "n": value} for key, value in counts.items()] or [{"": "—", "n": 0}]
