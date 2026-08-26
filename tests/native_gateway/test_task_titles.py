"""Russian executive titles: cache mapping, fallback, producer integration."""

from __future__ import annotations

import json
from pathlib import Path

from native_gateway.task_titles import clean_title, load_cache, title_for


def test_cache_hit_by_record_id_wins():
    cache = {"NEW-1": "Исправление публикации релиза", "Fix release publish": "другое"}
    assert (
        title_for("NEW-1", "Fix release publish", cache)
        == "Исправление публикации релиза"
    )


def test_cache_hit_by_slug_title():
    cache = {"Fix release publish": "Исправление публикации релиза"}
    assert (
        title_for("NEW-2", "Fix release publish", cache)
        == "Исправление публикации релиза"
    )


def test_missing_record_falls_back_to_slug_title():
    assert title_for("NEW-3", "Fix release publish", {}) == "Fix release publish"


def test_clean_title_bounds_and_strips():
    assert clean_title("  «Добавление  входа»\n") == "Добавление входа"
    long = "х" * 200
    cleaned = clean_title(long)
    assert len(cleaned) <= 90 and cleaned.endswith("…")


def test_load_cache_tolerates_garbage(tmp_path: Path):
    assert load_cache(None) == {}
    missing = tmp_path / "nope.json"
    assert load_cache(missing) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_cache(bad) == {}
    mixed = tmp_path / "mixed.json"
    mixed.write_text(json.dumps({"a": "ok", "b": 5, "c": "  "}), encoding="utf-8")
    assert load_cache(mixed) == {"a": "ok"}


def test_producer_applies_titles_cache(tmp_path: Path, monkeypatch):
    from native_gateway.projection_producer import build_projection

    root = tmp_path / "aicc"
    (root / "data").mkdir(parents=True)
    (root / "data/tasks.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("AICC_DATA_DIR", str(root / "data"))

    backlog = tmp_path / "backlog.md"
    backlog.write_text(
        "- VOYN_RECOMMENDATION | ts=2026-08-26T00:00:00Z | status=PO-Approved | "
        "issue_id=NEW-9001 | current_wave=W4 | proposed_wave=W4 | priority=P1 | "
        "owner=aicc | effect=high | effort=M | acceptance=accept:x | "
        "task=fix_release_publish_step | evidence=none | file_scope=NONE | "
        "parallel_domain=platform\n",
        encoding="utf-8",
    )
    titles = tmp_path / "titles_ru.json"
    titles.write_text(
        json.dumps(
            {"NEW-9001": "Исправление шага публикации релиза"}, ensure_ascii=False
        ),
        encoding="utf-8",
    )

    projection = build_projection(root, backlog_path=backlog, titles_path=titles)
    task = next(t for t in projection["tasks"] if t["id"] == "NEW-9001")
    assert task["title"] == "Исправление шага публикации релиза"

    untranslated = build_projection(root, backlog_path=backlog, titles_path=None)
    task2 = next(t for t in untranslated["tasks"] if t["id"] == "NEW-9001")
    assert task2["title"] == "Fix release publish step"
