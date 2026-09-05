"""Human titles for the executive projection.

The canonical backlog's machine records carry English snake_case slugs
(`aicc_native_phase0_audit_adr_contract_and_prototype`). The owner reads the
native apps in Russian, so the projection maps every slug to a short Russian
action phrase — "что делается и чего конкретно".

The mapping lives in a JSON cache file (`{record_id_or_slug: title}`) that is
produced OFFLINE by `native_gateway.localize_titles` (local Ollama model —
paid models are not spent on this; see the local-model delegation policy).
The producer only reads the cache: no model call ever happens on the serving
path, so projection builds stay fast and deterministic. A record missing
from the cache falls back to the humanized slug — honest English rather than
a half-translated guess — and shows up in the localizer's TODO on its next
run.
"""

from __future__ import annotations

import json
from pathlib import Path

_MAX_TITLE_LEN = 90


def load_cache(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value).strip()
        for key, value in raw.items()
        if isinstance(value, str) and value.strip()
    }


def clean_title(text: str) -> str:
    """One line, bounded length, no wrapping quotes."""
    line = " ".join(text.split())
    line = line.strip().strip('"«»“”').strip()
    if len(line) > _MAX_TITLE_LEN:
        line = line[: _MAX_TITLE_LEN - 1].rstrip() + "…"
    return line


def title_for(record_id: str, slug_title: str, cache: dict[str, str]) -> str:
    """Cache by record id first (stable), then by the humanized slug."""
    for key in (record_id, slug_title):
        cached = cache.get(key)
        if cached:
            return clean_title(cached)
    return slug_title
