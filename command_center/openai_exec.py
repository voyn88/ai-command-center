"""Model-only executor bridge to OpenAI-compatible HTTP providers.

VOYN-W0-AICC-GROQ-VERDICT-BENCH (owner priority 2026-09-03): the review
cascade's paid links share two exhaustible pools (the Claude subscription
window and the codex account), and both were measured exhausted on the same
day. Free OpenAI-compatible tiers (Groq, OpenRouter, Mistral) are capacity
neither pool can consume — but they are HTTP APIs, and every executor the
worker knows how to run is an argv subprocess whose stdout carries the
``VERDICT:``/``HEAD_SHA:`` trailer contract. This module is the smallest
honest adapter between those two facts: a CLI that reads one prompt, makes
one chat completion, prints the model's text to stdout, and exits nonzero
on anything else. The worker runs it exactly like ``claude -p``/``codex
exec`` (see ``agent_runner.build_openai_http_command``); nothing downstream
learns a second result format.

Model-only by construction, not by configuration: there is no tool loop, no
filesystem access, no network beyond the single provider call — so the only
task types this bridge may serve are the MODEL_ONLY ones whose prompt
already embeds the exact bytes under review (the trusted control plane
builds that envelope; ``review_merge._REVIEW_PROMPT`` forbids the reviewer
network access anyway). ``main`` re-validates the task type even though the
argv builder already refused wrong ones — the builder can be bypassed by a
hand-typed command; this check cannot.

Provider routing is by model prefix, and the table is deliberately closed:
an unknown prefix is a refusal, never a guess at a default endpoint —
sending a review envelope to an endpoint nobody named is exfiltration, not
failover. Keys come only from the environment (the fleet's ``.env`` on the
control/worker hosts); a missing key is a loud preflight-shaped exit, not a
retry, so a cascade link without its credential fails its attempt honestly.

The explicit User-Agent is load-bearing: Groq's edge (Cloudflare) rejects
python-urllib's default UA with an opaque 403 error 1010 — live-measured
2026-09-03 while the /models endpoint worked from curl with identical
credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

__all__ = ["PROVIDERS", "main", "resolve_provider", "run_completion"]

#: provider prefix -> (base URL, environment variable holding the key).
#: Closed table; see module docstring for why unknown prefixes refuse.
PROVIDERS: dict[str, tuple[str, str]] = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
}

#: Task types this bridge may serve — mirrors agent_runner.MODEL_ONLY_TASK_TYPES
#: by value rather than import so the module stays runnable standalone on a
#: worker host; the equality is pinned by a test, not by trust. Deliberately
#: NOT verification_review: verification resolves to the read-only REPO
#: profile (the verifier must read the tree), which a toolless HTTP bridge
#: cannot honestly serve.
MODEL_ONLY_TASK_TYPES = frozenset({"independent_review"})

_USER_AGENT = "aicc-openai-exec/1 (+voyn88/ai-command-center)"
_TIMEOUT_SECONDS = 300
#: Reasoning models (gpt-oss, magistral, nemotron) spend output budget on
#: thought before the trailer; a small cap yields an empty answer that looks
#: like a refusal (live-measured on gpt-oss-120b at 900 tokens). Generous by
#: default, still bounded.
_MAX_TOKENS = 16_384


def resolve_provider(model: str) -> tuple[str, str, str]:
    """Split ``provider/model-name`` into (base_url, api_key, bare_model).

    Raises ``ValueError`` for an unknown or missing prefix and
    ``LookupError`` when the provider's key is not in the environment —
    distinct types so ``main`` can exit with distinct diagnostics.
    """
    prefix, sep, bare = model.partition("/")
    if not sep or prefix not in PROVIDERS:
        raise ValueError(
            f"unknown provider prefix in model {model!r}; "
            f"known: {', '.join(sorted(PROVIDERS))}"
        )
    base_url, env_name = PROVIDERS[prefix]
    key = os.environ.get(env_name, "")
    if not key:
        raise LookupError(f"{env_name} is not set; cannot use {model!r}")
    return base_url, key, bare


def run_completion(model: str, prompt: str) -> str:
    """One chat completion, temperature 0, returning the message content.

    Any transport error, non-2xx status, or a response without non-empty
    content raises ``RuntimeError`` with a bounded diagnostic — the caller
    (and the worker's failure classifier) must never mistake an empty or
    malformed reply for a model verdict.
    """
    base_url, key, bare_model = resolve_provider(model)
    body = json.dumps(
        {
            "model": bare_model,
            "temperature": 0,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        raise RuntimeError(f"provider HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"provider transport failure: {exc}") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"provider response shape unrecognised: {str(payload)[:300]}"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            "provider returned empty content (reasoning budget exhausted or "
            "refusal); not a verdict"
        )
    return content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicc-openai-exec")
    parser.add_argument("--model", required=True)
    parser.add_argument("--task-type", required=True)
    parser.add_argument("prompt", help="the full prompt, or '-' to read stdin")
    args = parser.parse_args(argv)

    if args.task_type not in MODEL_ONLY_TASK_TYPES:
        print(
            f"openai_exec refuses task type {args.task_type!r}: this bridge "
            "has no tools, no workspace and no business running anything but "
            "model-only reviews",
            file=sys.stderr,
        )
        return 2

    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not prompt.strip():
        print("empty prompt", file=sys.stderr)
        return 2

    try:
        content = run_completion(args.model, prompt)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    sys.stdout.write(content)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
