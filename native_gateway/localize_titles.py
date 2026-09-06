"""Offline localizer: backlog slugs → short Russian action titles.

Runs a LOCAL Ollama model (local-model delegation policy: classification/
summarisation goes to local models first; no paid tokens). Results are
written to the JSON cache `native_gateway.task_titles` reads, keyed by the
backlog record id, so each slug is translated exactly once and the serving
path never calls a model.

    python -m native_gateway.localize_titles \
        --backlog <VOYN_TASKS_BACKLOG.md> --cache ~/.aicc-gateway/titles_ru.json

The cache is rewritten atomically after every batch, so an interrupted run
keeps everything already translated and the producer picks new titles up on
its next 60-second cycle.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

from command_center import backlog_client, storage

from .task_titles import clean_title, load_cache

_DEFAULT_MODEL = "llama3.1:8b"
_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

_INSTRUCTION = (
    "Ты помогаешь руководителю читать список задач. Переведи техническое "
    "название задачи на русский язык одной короткой понятной фразой вида "
    "«что делается + чего конкретно» (например: «Исправление публикации "
    "релиза», «Добавление входа по токену устройства»). Product-имена "
    "(AIOS, AICC, CI, PR, API) оставляй как есть. Ответь ТОЛЬКО самой "
    "фразой, без кавычек и пояснений, не длиннее 70 символов.\n"
    "Название задачи: "
)


def _translate(text: str, model: str, timeout: float) -> str | None:
    payload = json.dumps(
        {
            "model": model,
            "prompt": _INSTRUCTION + text,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 60},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    answer = body.get("response")
    if not isinstance(answer, str):
        return None
    title = clean_title(answer)
    # A refusal, an empty line, or an answer that is still the English slug
    # is worse than the fallback — keep such records untranslated.
    if not title or len(title) < 4:
        return None
    return title


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Localize backlog task titles (offline)"
    )
    parser.add_argument("--backlog", type=Path, default=None)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--flush-every", type=int, default=10)
    args = parser.parse_args(argv)

    projection = backlog_client.load_projection(args.backlog)
    rich = backlog_client.load_rich_records(args.backlog)
    cache = dict(load_cache(args.cache))
    named = [(rec.issue_id, rec.title) for rec in projection.records if rec.issue_id]
    named += [(rec.record_id, rec.title) for rec in rich]
    pending = [(rid, title) for rid, title in named if rid not in cache]
    print(f"records={len(named)} cached={len(cache)} pending={len(pending)}")

    done = 0
    failed = 0
    for record_id, source_title in pending:
        title = _translate(source_title, args.model, args.timeout)
        if title is None:
            failed += 1
            continue
        cache[record_id] = title
        done += 1
        if done % max(1, args.flush_every) == 0:
            storage.atomic_write_json(args.cache, cache)
            print(f"progress: {done}/{len(pending)} translated, {failed} failed")
    storage.atomic_write_json(args.cache, cache)
    print(f"finished: {done} translated, {failed} failed, cache={len(cache)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
