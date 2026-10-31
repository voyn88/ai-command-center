"""Provider-specific contracts for the shared Session Supervisor.

Providers build commands and create one stateful runtime per launched process.
That runtime is the single pre-persistence boundary for stream sanitization,
event normalization, readiness/result evidence, and bounded error evidence.
The Supervisor remains the sole process/state/timeout/cancellation owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from command_center import agent_runner
from command_center.runtime import stream_parser

CLAUDE_ID = "claude_code"
CODEX_ID = "codex"
OLLAMA_ID = "ollama"
COPILOT_ID = "copilot_cli"

# Failure reasons that mean the *executor itself* is unavailable — the provider
# cannot run this task at all right now, as opposed to the task being too hard
# or the agent refusing for content/policy reasons. When a run ends with one of
# these, the autopilot records the dead executor in
# ``task["failed_executors"]`` (task_sync) and lets the scheduler fall through
# to the next available agent (see ``execution_queue.select_available_executor``)
# instead of stranding the task or burning the retry budget retrying the same
# dead provider.
#
# Deliberately *excludes*:
#   ``incomplete:working_tree_unchanged``  — task content, not the provider;
#   ``blocked:permission_denied:*``        — environment policy, identical on
#                                            every executor, so switching helps
#                                            no one;
#   ``blocked:final_response:*``           — the agent judged the task blocked;
#   ``timeout`` / "process killed manually" — ambiguous: could be the task,
#                                            the machine, or the provider, so
#                                            retry budget (not failover) governs.
PROVIDER_UNAVAILABLE_REASONS: frozenset[str] = frozenset(
    {
        "session_expired",
        "authentication_failed",
        "quota_limit",
        "provider_api_error",
        "provider_exit_nonzero",
        "provider_launch_failed",
        "executable_missing",
        "network_error",
    }
)

MAX_PERSISTED_EVENT_CHARS = 65_536
MAX_CODEX_PROMPT_CHARS = 100_000

# Ollama runs a local model with a fixed context window and no retrieval, so an
# oversized prompt is silently truncated by the runner rather than refused. A
# lower ceiling than Codex's keeps the failure explicit and on our side.
MAX_OLLAMA_PROMPT_CHARS = 32_000
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"
MAX_COPILOT_PROMPT_CHARS = 100_000
MAX_REDACTION_SOURCE_CHARS = 16_384
MAX_PROMPT_PATTERNS = 96
MAX_CREDENTIAL_CHARS = 512
MAX_STREAM_CARRY_CHARS = 2_048
MIN_PROMPT_FRAGMENT_CHARS = 8
MAX_ENV_SECRET_VALUES = 64
MAX_ENV_SECRET_CHARS = 16_384
_ROLLING_HASH_MASK = (1 << 64) - 1
_ROLLING_HASH_BASE = 257


@dataclass(frozen=True)
class ProviderAvailability:
    provider_id: str
    available: bool
    code: str
    message: str
    executable: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    environment: dict[str, str]
    stdin_text: str | None
    audit_metadata: dict[str, object]


class ProviderRuntime(Protocol):
    requires_valid_result: bool
    requires_verified_identity: bool

    def feed_stdout(self, chunk: str) -> list[str]: ...

    def feed_stderr(self, chunk: str) -> list[str]: ...

    def flush_stdout(self) -> list[str]: ...

    def flush_stderr(self) -> list[str]: ...

    def parse_stdout_line(self, line: str) -> dict | None: ...

    def stdout_event_is_readiness(self, line: str, event: dict | None) -> bool: ...

    def stderr_line_is_readiness(self, line: str) -> bool: ...

    def event_is_valid_result(self, event: dict) -> bool: ...

    def event_is_provider_error(self, event: dict) -> bool: ...


class ExecutionProvider(Protocol):
    id: str
    label: str
    supports_resume: bool
    requires_dedicated_worktree: bool

    def availability(self) -> ProviderAvailability: ...

    def validate_prompt(self, prompt: str) -> None: ...

    def build_launch(
        self,
        *,
        repository_path: Path,
        session_id: str,
        prompt: str,
        task_type: str,
        is_resume: bool,
        model: str | None,
        untrusted: bool = False,
        operator_elevated: bool = False,
        capability_override: str | None = None,
    ) -> LaunchSpec: ...

    def create_runtime(self, *, prompt: str, environment: dict[str, str]) -> ProviderRuntime: ...

    def classify_failure(self, *, exit_code: int, diagnostic_lines: list[str]) -> str | None: ...


def _prompt_audit(prompt: str, transport: str) -> dict[str, object]:
    encoded = prompt.encode("utf-8")
    return {
        "prompt_transport": transport,
        "prompt_sha256": hashlib.sha256(encoded).hexdigest(),
        "prompt_bytes": len(encoded),
    }


# Availability probing shells out (two subprocesses per provider), and callers
# ask for it far more often than the answer can change: `scheduler.
# default_registry()` reads it for every provider on every autopilot tick, and
# the desktop refresh path runs that every 2-5 seconds. An uncached probe turns
# a planning decision into a steady stream of process spawns.
#
# The TTL is deliberately short. Installing a CLI, starting a local daemon or
# authenticating are exactly the actions an operator takes *while watching this
# screen*, so "available" must become true again within seconds, not on restart.
_PROBE_CACHE_TTL_SECONDS = 30.0
_probe_cache: dict[tuple[str, tuple[str, ...]], tuple[float, tuple[bool, str]]] = {}
_probe_cache_lock = threading.Lock()


def clear_probe_cache() -> None:
    """Drop every memoized probe. Tests that swap a provider binary mid-run
    call this so the next probe observes the new state immediately."""
    with _probe_cache_lock:
        _probe_cache.clear()


def _probe(executable: str, args: list[str], *, provider_id: str) -> tuple[bool, str]:
    """Memoized front for `_probe_uncached` — see `_PROBE_CACHE_TTL_SECONDS`."""
    key = (executable, tuple(args))
    now = time.monotonic()
    with _probe_cache_lock:
        cached = _probe_cache.get(key)
        if cached is not None and now - cached[0] < _PROBE_CACHE_TTL_SECONDS:
            return cached[1]
    result = _probe_uncached(executable, args, provider_id=provider_id)
    with _probe_cache_lock:
        _probe_cache[key] = (now, result)
    return result


def _probe_uncached(executable: str, args: list[str], *, provider_id: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [executable, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{provider_id} version/interface probe failed: {type(exc).__name__}"
    # Current Codex can emit a non-fatal warning on stderr while returning the
    # real version/help on stdout. Prefer stdout, but accept stderr-only output
    # from a successful probe.
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    if result.returncode != 0:
        return False, f"{provider_id} version/interface probe failed (exit {result.returncode})"
    return True, output


def _same_length_mask(value: str) -> str:
    """Mask a span without moving newline or JSON-string boundaries."""
    marker = "[REDACTED]"
    parts = re.split(r"(\r\n|\r|\n)", value)
    masked: list[str] = []
    for part in parts:
        if part in {"\r\n", "\r", "\n"}:
            masked.append(part)
        elif part:
            repeats = (len(part) + len(marker) - 1) // len(marker)
            masked.append((marker * repeats)[: len(part)])
    return "".join(masked)


_SENSITIVE_ENVIRONMENT_KEY = re.compile(
    r"(?i)(?:secret|token|api[_-]?key|password|credential|authorization|bearer)"
)


def _sensitive_environment_values(environment: dict[str, str]) -> tuple[str, ...]:
    values = tuple(
        dict.fromkeys(
            value
            for key, value in environment.items()
            if _SENSITIVE_ENVIRONMENT_KEY.search(key) and isinstance(value, str) and len(value) >= 4
        )
    )
    total_chars = sum(len(value) for value in values)
    if len(values) > MAX_ENV_SECRET_VALUES or total_chars > MAX_ENV_SECRET_CHARS:
        raise ValueError("Codex sensitive-environment redaction sources exceed the safety bound.")
    return values


class SensitiveValueRedactor:
    """Deterministic, bounded prompt/credential span redaction.

    Prompt-derived source values live only in this in-memory object. They are
    never included in audit metadata or persisted events.
    """

    _VALUE_PREFIX = r"(?:^|[^A-Za-z0-9]|\\[nrt])"
    _SK_TOKEN = re.compile(
        rf"(?i){_VALUE_PREFIX}(?P<secret>sk-(?:[A-Za-z0-9_-]|\r?\n)"
        rf"{{8,{MAX_CREDENTIAL_CHARS}}})"
    )
    _BEARER = re.compile(
        rf"(?i){_VALUE_PREFIX}bearer[ \t]+"
        rf"(?P<secret>[A-Za-z0-9._~+/=-](?:[A-Za-z0-9._~+/=-]|\r?\n)"
        rf"{{3,{MAX_CREDENTIAL_CHARS - 1}}})"
    )
    _ASSIGNMENT = re.compile(
        rf"(?i){_VALUE_PREFIX}(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token)"
        rf"(?:\\?[\"'])*\s*(?::|=|\\\":)\s*(?:\\?[\"'])*\s*"
        rf"(?P<secret>[A-Za-z0-9._~+/=-](?:[A-Za-z0-9._~+/=-]|\r?\n)"
        rf"{{2,{MAX_CREDENTIAL_CHARS - 1}}})"
    )
    _PROMPT_TOKEN = re.compile(r"[A-Za-z0-9_./+=:@-]{8,}")

    def __init__(self, prompt: str, sensitive_values: tuple[str, ...] = ()) -> None:
        if len(prompt) > MAX_CODEX_PROMPT_CHARS:
            raise ValueError(
                f"Codex prompt exceeds the {MAX_CODEX_PROMPT_CHARS}-character safety limit."
            )
        patterns: list[str] = []

        def add(value: str) -> None:
            value = value.strip("\r\n")
            if len(value) < 4 or value in patterns:
                return
            current_size = sum(len(item) for item in patterns)
            if len(patterns) >= MAX_PROMPT_PATTERNS or current_size + len(value) > MAX_REDACTION_SOURCE_CHARS:
                return
            patterns.append(value)

        add(prompt)
        for sensitive_value in sensitive_values:
            add(sensitive_value)
            add(json.dumps(sensitive_value, ensure_ascii=False)[1:-1])
        for line in prompt.splitlines():
            stripped = line.strip()
            if 8 <= len(stripped) <= 512:
                add(stripped)
            for match in self._PROMPT_TOKEN.finditer(stripped):
                add(match.group(0)[:256])
        self._prompt_patterns = tuple(patterns)
        # Fixed-size rolling fingerprints cover arbitrary prompt fragments,
        # including fragments from JSON-escaped prompt echoes, without
        # persisting source text or retaining an unbounded pattern list.
        fragment_sources = (prompt, json.dumps(prompt, ensure_ascii=False)[1:-1], *sensitive_values)
        self._prompt_fragment_hashes: set[int] = set()
        for source in fragment_sources:
            self._prompt_fragment_hashes.update(self._window_hashes(source))
            self._prompt_fragment_hashes.update(
                self._window_hashes(json.dumps(source, ensure_ascii=False)[1:-1])
            )

    @staticmethod
    def _window_hashes(value: str) -> set[int]:
        width = MIN_PROMPT_FRAGMENT_CHARS
        if len(value) < width:
            return set()
        factor = pow(_ROLLING_HASH_BASE, width - 1, 1 << 64)
        current = 0
        for character in value[:width]:
            current = ((current * _ROLLING_HASH_BASE) + ord(character)) & _ROLLING_HASH_MASK
        hashes = {current}
        for index in range(width, len(value)):
            current = (
                (current - (ord(value[index - width]) * factor))
                * _ROLLING_HASH_BASE
                + ord(value[index])
            ) & _ROLLING_HASH_MASK
            hashes.add(current)
        return hashes

    def _prompt_fragment_spans(self, text: str) -> list[tuple[int, int]]:
        width = MIN_PROMPT_FRAGMENT_CHARS
        if len(text) < width or not self._prompt_fragment_hashes:
            return []
        factor = pow(_ROLLING_HASH_BASE, width - 1, 1 << 64)
        current = 0
        for character in text[:width]:
            current = ((current * _ROLLING_HASH_BASE) + ord(character)) & _ROLLING_HASH_MASK
        spans = [(0, width)] if current in self._prompt_fragment_hashes else []
        for index in range(width, len(text)):
            current = (
                (current - (ord(text[index - width]) * factor))
                * _ROLLING_HASH_BASE
                + ord(text[index])
            ) & _ROLLING_HASH_MASK
            if current in self._prompt_fragment_hashes:
                spans.append((index - width + 1, index + 1))
        return spans

    def spans(self, text: str) -> list[tuple[int, int]]:
        spans = self._prompt_fragment_spans(text)
        for value in self._prompt_patterns:
            start = 0
            while True:
                index = text.find(value, start)
                if index < 0:
                    break
                spans.append((index, index + len(value)))
                start = index + max(1, len(value))
        for pattern in (self._SK_TOKEN, self._BEARER, self._ASSIGNMENT):
            for match in pattern.finditer(text):
                spans.append(match.span("secret"))
        if not spans:
            return []
        spans.sort()
        merged = [spans[0]]
        for start, end in spans[1:]:
            previous_start, previous_end = merged[-1]
            if start <= previous_end:
                merged[-1] = (previous_start, max(previous_end, end))
            else:
                merged.append((start, end))
        return merged

    def redact(self, text: str, *, existing_mask: list[bool] | None = None) -> tuple[str, list[bool]]:
        mask = list(existing_mask or [False] * len(text))
        if len(mask) < len(text):
            mask.extend([False] * (len(text) - len(mask)))
        for start, end in self.spans(text):
            for index in range(max(0, start), min(len(text), end)):
                mask[index] = True
        output: list[str] = []
        index = 0
        while index < len(text):
            if not mask[index]:
                output.append(text[index])
                index += 1
                continue
            end = index + 1
            while end < len(text) and mask[end]:
                end += 1
            output.append(_same_length_mask(text[index:end]))
            index = end
        return "".join(output), mask


def _split_chunks(text: str) -> list[str]:
    chunks = text.splitlines(keepends=True)
    return chunks or ([text] if text else [])


class _BufferedSanitizedStream:
    """Bounded carry-over catches values split across chunks or lines."""

    def __init__(self, redactor: SensitiveValueRedactor) -> None:
        self._redactor = redactor
        self._pending = ""
        self._pending_mask: list[bool] = []

    def feed(self, chunk: str, *, force_flush: bool = False) -> list[str]:
        pieces = [
            chunk[offset : offset + MAX_PERSISTED_EVENT_CHARS]
            for offset in range(0, len(chunk), MAX_PERSISTED_EVENT_CHARS)
        ] or [""]
        emitted: list[str] = []
        for piece in pieces:
            self._pending += piece
            self._pending_mask.extend([False] * len(piece))
            sanitized, self._pending_mask = self._redactor.redact(
                self._pending, existing_mask=self._pending_mask
            )
            if force_flush:
                emitted.extend(_split_chunks(sanitized))
                self._pending = ""
                self._pending_mask = []
                continue
            if len(self._pending) <= MAX_STREAM_CARRY_CHARS:
                continue
            cut = len(self._pending) - MAX_STREAM_CARRY_CHARS
            # Preserve event boundaries whenever possible. An oversized
            # malformed line is still emitted in bounded pieces.
            newline_cut = self._pending.rfind("\n", 0, cut)
            if newline_cut >= 0:
                cut = newline_cut + 1
            emitted.extend(_split_chunks(sanitized[:cut]))
            self._pending = self._pending[cut:]
            self._pending_mask = self._pending_mask[cut:]
        return emitted

    def flush(self) -> list[str]:
        if not self._pending:
            return []
        sanitized, _ = self._redactor.redact(self._pending, existing_mask=self._pending_mask)
        self._pending = ""
        self._pending_mask = []
        return _split_chunks(sanitized)


class SanitizationBoundary:
    def __init__(self, prompt: str, sensitive_values: tuple[str, ...] = ()) -> None:
        redactor = SensitiveValueRedactor(prompt, sensitive_values)
        self._stdout = _BufferedSanitizedStream(redactor)
        self._stderr = _BufferedSanitizedStream(redactor)

    def feed_stdout(self, chunk: str) -> list[str]:
        # Complete JSON events are self-contained provider messages. Flush
        # them promptly so readiness remains observable during a live run.
        # Any preceding malformed carry is sanitized together with this line
        # before either part is released.
        complete_json_object = False
        if len(chunk) <= MAX_PERSISTED_EVENT_CHARS:
            try:
                complete_json_object = isinstance(json.loads(chunk), dict)
            except (json.JSONDecodeError, ValueError):
                pass
        return self._stdout.feed(chunk, force_flush=complete_json_object)

    def feed_stderr(self, chunk: str) -> list[str]:
        return self._stderr.feed(chunk)

    def flush_stdout(self) -> list[str]:
        return self._stdout.flush()

    def flush_stderr(self) -> list[str]:
        return self._stderr.flush()


class _PassthroughBoundary:
    @staticmethod
    def feed_stdout(chunk: str) -> list[str]:
        return [chunk]

    @staticmethod
    def feed_stderr(chunk: str) -> list[str]:
        return [chunk]

    @staticmethod
    def flush_stdout() -> list[str]:
        return []

    @staticmethod
    def flush_stderr() -> list[str]:
        return []


class ClaudeRuntime:
    requires_valid_result = False
    requires_verified_identity = False

    def __init__(self) -> None:
        self._boundary = _PassthroughBoundary()

    def feed_stdout(self, chunk: str) -> list[str]:
        return self._boundary.feed_stdout(chunk)

    def feed_stderr(self, chunk: str) -> list[str]:
        return self._boundary.feed_stderr(chunk)

    def flush_stdout(self) -> list[str]:
        return []

    def flush_stderr(self) -> list[str]:
        return []

    @staticmethod
    def parse_stdout_line(line: str) -> dict | None:
        return stream_parser.parse_stream_line(line)

    @staticmethod
    def stdout_event_is_readiness(line: str, event: dict | None) -> bool:
        return bool(line)

    @staticmethod
    def stderr_line_is_readiness(line: str) -> bool:
        return bool(line)

    @staticmethod
    def event_is_valid_result(event: dict) -> bool:
        return event.get("event_type") == "result"

    @staticmethod
    def event_is_provider_error(event: dict) -> bool:
        """A `result` carrying `is_error` is the CLI reporting that the turn
        failed on its side — an expired session, an exhausted quota, an API
        fault. Treating it as diagnostic evidence is what lets
        `classify_failure` turn a bare exit code into an actionable reason;
        without it the operator sees only "FAILED" for a run that never even
        reached the model."""
        return bool(event.get("event_type") == "result" and event.get("payload", {}).get("is_error"))


class CodexRuntime:
    requires_valid_result = True
    requires_verified_identity = True

    def __init__(self, prompt: str, sensitive_values: tuple[str, ...] = ()) -> None:
        self._boundary = SanitizationBoundary(prompt, sensitive_values)
        self._last_assistant_text = ""

    def feed_stdout(self, chunk: str) -> list[str]:
        return self._boundary.feed_stdout(chunk)

    def feed_stderr(self, chunk: str) -> list[str]:
        return self._boundary.feed_stderr(chunk)

    def flush_stdout(self) -> list[str]:
        return self._boundary.flush_stdout()

    def flush_stderr(self) -> list[str]:
        return self._boundary.flush_stderr()

    def parse_stdout_line(self, line: str) -> dict | None:
        if len(line) > MAX_PERSISTED_EVENT_CHARS:
            return {
                "event_type": "malformed",
                "payload": {
                    "raw": line[:MAX_PERSISTED_EVENT_CHARS],
                    "error": "provider event exceeded persistence bound",
                },
            }
        parsed = stream_parser.parse_stream_line(line)
        if parsed is None or parsed["event_type"] == "malformed":
            return parsed
        payload = parsed["payload"]
        msg_type = payload.get("type")
        if msg_type in {"thread.started", "turn.started"}:
            normalized = {"provider_event": msg_type}
            if isinstance(payload.get("thread_id"), str):
                normalized["thread_id"] = payload["thread_id"][:256]
            return {"event_type": "lifecycle", "payload": normalized}
        if msg_type == "item.completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    self._last_assistant_text = text[:MAX_PERSISTED_EVENT_CHARS]
                    return {
                        "event_type": "assistant_message",
                        "payload": {
                            "provider": CODEX_ID,
                            "message": {"content": [{"type": "text", "text": self._last_assistant_text}]},
                        },
                    }
            if isinstance(item, dict) and item.get("type") == "error":
                return self._provider_error("item.completed", item)
        if msg_type == "turn.completed":
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
            return {
                "event_type": "result",
                "payload": {
                    "provider": CODEX_ID,
                    "provider_event": msg_type,
                    "provider_completion_valid": bool(self._last_assistant_text),
                    "result": self._last_assistant_text,
                    "usage": usage,
                },
            }
        if msg_type in {"error", "turn.failed"}:
            return self._provider_error(msg_type, payload)
        return parsed

    @staticmethod
    def _provider_error(event_name: str, payload: dict) -> dict:
        raw_error = payload.get("error")
        if isinstance(raw_error, dict):
            message = raw_error.get("message") or json.dumps(raw_error, ensure_ascii=False, sort_keys=True)
            code = raw_error.get("code")
        else:
            message = raw_error or payload.get("message") or payload.get("detail") or event_name
            code = payload.get("code")
        return {
            "event_type": "provider_error",
            "payload": {
                "provider": CODEX_ID,
                "provider_event": event_name,
                "code": str(code)[:128] if code is not None else None,
                "message": str(message)[:MAX_PERSISTED_EVENT_CHARS],
            },
        }

    @staticmethod
    def stdout_event_is_readiness(line: str, event: dict | None) -> bool:
        return bool(
            event
            and event.get("event_type") == "lifecycle"
            and (event.get("payload") or {}).get("provider_event") in {"thread.started", "turn.started"}
        )

    @staticmethod
    def stderr_line_is_readiness(line: str) -> bool:
        return False

    @staticmethod
    def event_is_valid_result(event: dict) -> bool:
        return bool(
            event.get("event_type") == "result"
            and (event.get("payload") or {}).get("provider_completion_valid")
        )

    @staticmethod
    def event_is_provider_error(event: dict) -> bool:
        return event.get("event_type") == "provider_error"


class ClaudeProvider:
    id = CLAUDE_ID
    label = "Claude Code"
    supports_resume = True
    requires_dedicated_worktree = False

    def availability(self) -> ProviderAvailability:
        binary = os.environ.get("AICC_CLAUDE_BINARY") or "claude"
        executable = shutil.which(binary) or binary
        return ProviderAvailability(self.id, True, "usable", "Claude Code CLI is configured.", executable)

    @staticmethod
    def validate_prompt(prompt: str) -> None:
        return None

    def build_launch(
        self,
        *,
        repository_path: Path,
        session_id: str,
        prompt: str,
        task_type: str,
        is_resume: bool,
        model: str | None,
        untrusted: bool = False,
        operator_elevated: bool = False,
        capability_override: str | None = None,
    ) -> LaunchSpec:
        from command_center.runtime import supervisor

        argv = supervisor.build_claude_command(
            session_id=session_id,
            prompt=prompt,
            task_type=task_type,
            is_resume=is_resume,
            model=model,
            untrusted=untrusted,
            operator_elevated=operator_elevated,
            capability_override=capability_override,
            prompt_in_argv=False,
        )
        return LaunchSpec(
            argv=tuple(argv),
            environment=dict(os.environ),
            stdin_text=prompt,
            audit_metadata={"provider_id": self.id, **_prompt_audit(prompt, "stdin")},
        )

    @staticmethod
    def create_runtime(*, prompt: str, environment: dict[str, str]) -> ProviderRuntime:
        return ClaudeRuntime()

    @staticmethod
    def parse_stdout_line(line: str) -> dict | None:
        return stream_parser.parse_stream_line(line)

    @staticmethod
    def sanitize_stderr(line: str) -> str:
        return line

    @staticmethod
    def classify_failure(*, exit_code: int, diagnostic_lines: list[str]) -> str | None:
        """Name the cause when the CLI itself reported one.

        `diagnostic_lines` is already sanitized and bounded by the Supervisor.
        The checks are ordered most-specific first: an expired session and an
        exhausted quota are both "authentication-adjacent" in wording, and
        conflating them would send the operator to the wrong remedy."""
        text = "\n".join(diagnostic_lines).lower()
        if not text:
            return "provider_exit_nonzero" if exit_code else None
        if "oauth" in text or "session expired" in text or "log in" in text or "login" in text:
            return "session_expired"
        if any(token in text for token in ("quota", "usage limit", "spend limit", "rate limit")):
            return "quota_limit"
        if any(token in text for token in ("authenticate", "authentication", "unauthorized", "api key")):
            return "authentication_failed"
        if "overloaded" in text or "api_error" in text or "api error" in text:
            return "provider_api_error"
        return "provider_exit_nonzero" if exit_code else None


class CodexProvider:
    id = CODEX_ID
    label = "Codex CLI"
    supports_resume = False
    requires_dedicated_worktree = True

    def _executable(self) -> str | None:
        configured = os.environ.get("AICC_CODEX_BINARY")
        if configured:
            path = Path(configured).expanduser()
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which("codex")

    def availability(self) -> ProviderAvailability:
        executable = self._executable()
        if executable is None:
            return ProviderAvailability(
                self.id, False, "executable_missing", "Codex CLI not found; install it or configure AICC_CODEX_BINARY."
            )
        version_ok, version = _probe(executable, ["--version"], provider_id=self.id)
        if not version_ok:
            return ProviderAvailability(self.id, False, "version_probe_failed", version, executable=executable)
        help_ok, help_output = _probe(executable, ["exec", "--help"], provider_id=self.id)
        required = ("Run Codex non-interactively", "--json", "--cd <DIR>", "read from stdin")
        if not help_ok or any(marker not in help_output for marker in required):
            return ProviderAvailability(
                self.id,
                False,
                "unsupported_interface",
                "Installed Codex CLI does not expose the required non-interactive stdin/JSON interface.",
                executable,
                version,
            )
        return ProviderAvailability(self.id, True, "usable", "Codex CLI is available.", executable, version)

    @staticmethod
    def validate_prompt(prompt: str) -> None:
        if len(prompt) > MAX_CODEX_PROMPT_CHARS:
            raise ValueError(
                f"Codex prompt exceeds the {MAX_CODEX_PROMPT_CHARS}-character safety limit."
            )
        _sensitive_environment_values(dict(os.environ))

    def build_launch(
        self,
        *,
        repository_path: Path,
        session_id: str,
        prompt: str,
        task_type: str,
        is_resume: bool,
        model: str | None,
        untrusted: bool = False,
        operator_elevated: bool = False,
        capability_override: str | None = None,
    ) -> LaunchSpec:
        if is_resume:
            raise ValueError("Codex CLI resume is not supported by this provider increment.")
        self.validate_prompt(prompt)
        environment = dict(os.environ)
        _sensitive_environment_values(environment)
        availability = self.availability()
        if not availability.available or not availability.executable:
            raise RuntimeError(availability.message)
        # Provenance-aware sandbox (audit SEC-D-01): resolve the same execution
        # profile the Claude path uses, so an untrusted (imported) non-read-only
        # task is downgraded to the read-only sandbox — no command execution, no
        # worktree writes — unless an operator has explicitly elevated it. Keying
        # the sandbox on `task_type` alone let a prompt-injected imported task
        # obtain `workspace-write` just by being run through the Codex executor.
        profile = agent_runner.profile_for_task(
            task_type, untrusted=untrusted, operator_elevated=operator_elevated
        )
        sandbox = "read-only" if profile == agent_runner.PROFILE_READ_ONLY else "workspace-write"
        argv = [
            availability.executable,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "--cd",
            str(repository_path),
        ]
        if model:
            argv.extend(["--model", model])
        argv.append("-")
        audit = {
            "provider_id": self.id,
            "provider_version": availability.version,
            "non_interactive": True,
            "sandbox": sandbox,
            "readiness": "recognized_codex_lifecycle_event",
            "result_evidence": "normalized_agent_message_then_turn_completed",
            "cancellation": "verified_process_group_sigterm_then_sigkill",
            **_prompt_audit(prompt, "stdin"),
        }
        return LaunchSpec(tuple(argv), environment, prompt, audit)

    @staticmethod
    def create_runtime(*, prompt: str, environment: dict[str, str]) -> ProviderRuntime:
        return CodexRuntime(prompt, _sensitive_environment_values(environment))

    def parse_stdout_line(self, line: str) -> dict | None:
        return CodexRuntime("").parse_stdout_line(line)

    @staticmethod
    def sanitize_stderr(line: str) -> str:
        runtime = CodexRuntime("")
        runtime.feed_stderr(line)
        return "".join(runtime.flush_stderr())

    @staticmethod
    def classify_failure(*, exit_code: int, diagnostic_lines: list[str]) -> str | None:
        # `diagnostic_lines` is already sanitized and bounded by Supervisor,
        # and contains only stderr/provider_error evidence.
        text = "\n".join(diagnostic_lines).lower()
        if any(token in text for token in ("quota", "usage limit", "spend limit", "rate limit")):
            return "quota_limit"
        if any(token in text for token in ("not logged in", "authentication", "unauthorized", "api key")):
            return "authentication_failed"
        return "provider_exit_nonzero" if exit_code else None



class OllamaRuntime:
    """Plain-text runtime: Ollama emits prose, not a structured event stream.

    `requires_valid_result = False` because there is no machine-checkable
    "the turn completed successfully" marker to require — the process exit code
    is the only completion signal the runner gives. `requires_verified_identity`
    stays True: identity verification protects against signalling a reused PID
    during cancellation, which has nothing to do with what the process prints.
    """

    requires_valid_result = False
    requires_verified_identity = True

    def __init__(self, prompt: str, sensitive_values: tuple[str, ...] = ()) -> None:
        self._boundary = SanitizationBoundary(prompt, sensitive_values)

    def feed_stdout(self, chunk: str) -> list[str]:
        return self._boundary.feed_stdout(chunk)

    def feed_stderr(self, chunk: str) -> list[str]:
        return self._boundary.feed_stderr(chunk)

    def flush_stdout(self) -> list[str]:
        return self._boundary.flush_stdout()

    def flush_stderr(self) -> list[str]:
        return self._boundary.flush_stderr()

    @staticmethod
    def parse_stdout_line(line: str) -> dict | None:
        """Every non-blank line is assistant text. Ollama has no event
        vocabulary to normalize, so nothing is invented here: the line is
        persisted as-is under the existing `assistant` event type."""
        text = line.rstrip("\n").rstrip("\r")
        if not text.strip():
            return None
        return {"event_type": "assistant", "payload": {"text": text}}

    @staticmethod
    def stdout_event_is_readiness(line: str, event: dict | None) -> bool:
        return bool(line.strip())

    @staticmethod
    def stderr_line_is_readiness(line: str) -> bool:
        # Ollama writes model-pull and load progress to stderr before the first
        # token. That proves the process is alive and working, which is exactly
        # what readiness means here.
        return bool(line.strip())

    @staticmethod
    def event_is_valid_result(event: dict) -> bool:
        return False

    @staticmethod
    def event_is_provider_error(event: dict) -> bool:
        return False


class OllamaProvider:
    """A local Ollama model as an execution provider.

    **Scope, stated plainly.** Ollama's stable non-interactive interface is
    `ollama run MODEL` — text in, text out. It has no file editing, no shell and
    no git. Tools exist only behind `--experimental`, whose companion flag
    `--experimental-yolo` skips every tool approval; neither is used here, and
    enabling them by default would hand an unsupervised local model write access
    to a repository.

    So this provider is deliberately restricted to **read-only task types**
    (`agent_runner.READ_ONLY_TASK_TYPES` — review, audit and the like). That is
    not a temporary limitation to be relaxed later by flipping a flag: a run
    that cannot modify the working tree can never satisfy an implementation
    task, and letting one be dispatched would produce a run that "succeeds"
    while changing nothing. Refusing at launch is the honest failure.

    Within that scope it is genuinely useful: an independent reviewer that costs
    nothing per token and never leaves the machine.
    """

    id = OLLAMA_ID
    label = "Ollama (local)"
    supports_resume = False
    # A read-only model cannot write, so it cannot corrupt a shared tree; the
    # dedicated-worktree requirement exists to stop two *writers* colliding.
    requires_dedicated_worktree = False

    def _executable(self) -> str | None:
        configured = os.environ.get("AICC_OLLAMA_BINARY")
        if configured:
            path = Path(configured).expanduser()
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which("ollama")

    def _model(self, model: str | None) -> str:
        return model or os.environ.get("AICC_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL

    def availability(self) -> ProviderAvailability:
        executable = self._executable()
        if executable is None:
            return ProviderAvailability(
                self.id,
                False,
                "executable_missing",
                "Ollama not found; install it or configure AICC_OLLAMA_BINARY.",
            )
        version_ok, version = _probe(executable, ["--version"], provider_id=self.id)
        if not version_ok:
            return ProviderAvailability(self.id, False, "version_probe_failed", version, executable=executable)
        # `ollama list` is the cheapest proof that the local daemon is actually
        # reachable. Without it the binary exists but every run would fail on
        # first token with a connection error.
        daemon_ok, _listing = _probe(executable, ["list"], provider_id=self.id)
        if not daemon_ok:
            return ProviderAvailability(
                self.id,
                False,
                "daemon_unreachable",
                "Ollama binary found but the local server is not reachable; start it with `ollama serve`.",
                executable,
                version,
            )
        return ProviderAvailability(self.id, True, "usable", "Ollama is available.", executable, version)

    @staticmethod
    def validate_prompt(prompt: str) -> None:
        if len(prompt) > MAX_OLLAMA_PROMPT_CHARS:
            raise ValueError(
                f"Ollama prompt exceeds the {MAX_OLLAMA_PROMPT_CHARS}-character limit; "
                "a local model would silently truncate it."
            )
        _sensitive_environment_values(dict(os.environ))

    def build_launch(
        self,
        *,
        repository_path: Path,
        session_id: str,
        prompt: str,
        task_type: str,
        is_resume: bool,
        model: str | None,
        untrusted: bool = False,
        operator_elevated: bool = False,
        capability_override: str | None = None,
    ) -> LaunchSpec:
        if is_resume:
            raise ValueError("Ollama resume is not supported: each run is a fresh, stateless completion.")
        if task_type not in agent_runner.READ_ONLY_TASK_TYPES:
            raise ValueError(
                f"Ollama cannot run task type {task_type!r}: it has no file-editing or shell "
                "capability, so it is restricted to read-only task types "
                f"({', '.join(sorted(agent_runner.READ_ONLY_TASK_TYPES))})."
            )
        self.validate_prompt(prompt)
        environment = dict(os.environ)
        _sensitive_environment_values(environment)
        availability = self.availability()
        if not availability.available or not availability.executable:
            raise RuntimeError(availability.message)

        resolved_model = self._model(model)
        argv = [
            availability.executable,
            "run",
            resolved_model,
            # Keep output a plain token stream: no reflowing that would corrupt
            # quoted diffs, and no chain-of-thought for reasoning models, which
            # is noise the reviewer's verdict must not be parsed out of.
            "--nowordwrap",
            "--hidethinking",
        ]
        audit = {
            "provider_id": self.id,
            "provider_version": availability.version,
            "model": resolved_model,
            "non_interactive": True,
            "sandbox": "read_only_no_tools",
            "readiness": "first_output_line",
            "result_evidence": "process_exit_zero",
            "cancellation": "verified_process_group_sigterm_then_sigkill",
            **_prompt_audit(prompt, "stdin"),
        }
        return LaunchSpec(tuple(argv), environment, prompt, audit)

    @staticmethod
    def create_runtime(*, prompt: str, environment: dict[str, str]) -> ProviderRuntime:
        return OllamaRuntime(prompt, _sensitive_environment_values(environment))

    def parse_stdout_line(self, line: str) -> dict | None:
        return OllamaRuntime("").parse_stdout_line(line)

    @staticmethod
    def classify_failure(*, exit_code: int, diagnostic_lines: list[str]) -> str | None:
        text = "\n".join(diagnostic_lines).lower()
        if any(token in text for token in ("connection refused", "could not connect", "dial tcp")):
            return "daemon_unreachable"
        if "not found" in text and "model" in text:
            return "model_missing"
        if any(token in text for token in ("out of memory", "insufficient memory", "cannot allocate")):
            return "insufficient_memory"
        return "provider_exit_nonzero" if exit_code else None


class CopilotRuntime:
    """JSONL event-stream runtime for GitHub Copilot CLI.

    Copilot CLI emits one JSON object per line when ``--output-format json``
    is used. Key events (the ``type`` field):

    - ``assistant.turn_start`` — readiness: the process is alive and the
      model turn has started.
    - ``assistant.message`` — a complete assistant message with
      ``data.content`` holding the text.
    - ``tool.execution_start`` / ``tool.execution_complete`` — tool calls
      (bash, file edits, etc.).
    - ``assistant.turn_end`` — the model turn finished.
    - ``result`` — the terminal event with ``exitCode`` and ``usage``.

    ``requires_valid_result = True`` because a non-empty
    ``assistant.message`` + ``assistant.turn_end`` is the machine-checkable
    "the turn completed successfully" marker — same contract Codex holds.
    """

    requires_valid_result = True
    requires_verified_identity = True

    def __init__(self, prompt: str, sensitive_values: tuple[str, ...] = ()) -> None:
        self._boundary = SanitizationBoundary(prompt, sensitive_values)
        self._last_assistant_text = ""
        self._turn_ended = False

    def feed_stdout(self, chunk: str) -> list[str]:
        return self._boundary.feed_stdout(chunk)

    def feed_stderr(self, chunk: str) -> list[str]:
        return self._boundary.feed_stderr(chunk)

    def flush_stdout(self) -> list[str]:
        return self._boundary.flush_stdout()

    def flush_stderr(self) -> list[str]:
        return self._boundary.flush_stderr()

    def parse_stdout_line(self, line: str) -> dict | None:
        if len(line) > MAX_PERSISTED_EVENT_CHARS:
            return {
                "event_type": "malformed",
                "payload": {
                    "raw": line[:MAX_PERSISTED_EVENT_CHARS],
                    "error": "provider event exceeded persistence bound",
                },
            }
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None
        msg_type = obj.get("type")
        data = obj.get("data") or {}
        if msg_type == "assistant.turn_start":
            return {"event_type": "lifecycle", "payload": {"provider_event": msg_type}}
        if msg_type == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content:
                self._last_assistant_text = content[:MAX_PERSISTED_EVENT_CHARS]
                return {
                    "event_type": "assistant_message",
                    "payload": {
                        "provider": COPILOT_ID,
                        "message": {"content": [{"type": "text", "text": self._last_assistant_text}]},
                    },
                }
            return None
        if msg_type == "assistant.turn_end":
            self._turn_ended = True
            return {
                "event_type": "result",
                "payload": {
                    "provider": COPILOT_ID,
                    "provider_event": msg_type,
                    "provider_completion_valid": bool(self._last_assistant_text),
                    "result": self._last_assistant_text,
                    "usage": data.get("usage") if isinstance(data.get("usage"), dict) else None,
                },
            }
        if msg_type == "result":
            exit_code = obj.get("exitCode")
            if isinstance(exit_code, int) and exit_code != 0:
                return {
                    "event_type": "provider_error",
                    "payload": {
                        "provider": COPILOT_ID,
                        "provider_event": "result",
                        "code": str(exit_code)[:128],
                        "message": f"Copilot CLI exited with code {exit_code}",
                    },
                }
            return None
        if msg_type in {"error", "assistant.error"}:
            message = data.get("message") or data.get("error") or msg_type
            return {
                "event_type": "provider_error",
                "payload": {
                    "provider": COPILOT_ID,
                    "provider_event": msg_type,
                    "code": str(data.get("code", ""))[:128] if data.get("code") else None,
                    "message": str(message)[:MAX_PERSISTED_EVENT_CHARS],
                },
            }
        return None

    @staticmethod
    def stdout_event_is_readiness(line: str, event: dict | None) -> bool:
        return bool(
            event
            and event.get("event_type") == "lifecycle"
            and (event.get("payload") or {}).get("provider_event") == "assistant.turn_start"
        )

    @staticmethod
    def stderr_line_is_readiness(line: str) -> bool:
        return bool(line.strip())

    @staticmethod
    def event_is_valid_result(event: dict) -> bool:
        return bool(
            event.get("event_type") == "result"
            and (event.get("payload") or {}).get("provider_completion_valid")
        )

    @staticmethod
    def event_is_provider_error(event: dict) -> bool:
        return event.get("event_type") == "provider_error"


class CopilotProvider:
    """GitHub Copilot CLI as an execution provider.

    Copilot CLI is a non-interactive coding agent that can read, search, edit
    files and run shell commands. Like Codex, it is launched in a
    PID-tracked process group via the Execution Center v2 supervisor, reads
    its prompt from stdin, and emits structured JSONL events on stdout.

    The prompt is passed via stdin (not ``-p``) so it never appears in
    ``argv`` — the same security property Codex holds.
    """

    id = COPILOT_ID
    label = "Copilot CLI"
    supports_resume = False
    requires_dedicated_worktree = True

    def _executable(self) -> str | None:
        configured = os.environ.get("AICC_COPILOT_BINARY")
        if configured:
            path = Path(configured).expanduser()
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which("copilot")

    def availability(self) -> ProviderAvailability:
        executable = self._executable()
        if executable is None:
            return ProviderAvailability(
                self.id, False, "executable_missing",
                "Copilot CLI not found; install it or configure AICC_COPILOT_BINARY.",
            )
        version_ok, version = _probe(executable, ["--version"], provider_id=self.id)
        if not version_ok:
            return ProviderAvailability(self.id, False, "version_probe_failed", version, executable=executable)
        return ProviderAvailability(self.id, True, "usable", "Copilot CLI is available.", executable, version)

    @staticmethod
    def validate_prompt(prompt: str) -> None:
        if len(prompt) > MAX_COPILOT_PROMPT_CHARS:
            raise ValueError(
                f"Copilot prompt exceeds the {MAX_COPILOT_PROMPT_CHARS}-character safety limit."
            )
        _sensitive_environment_values(dict(os.environ))

    def build_launch(
        self,
        *,
        repository_path: Path,
        session_id: str,
        prompt: str,
        task_type: str,
        is_resume: bool,
        model: str | None,
        untrusted: bool = False,
        operator_elevated: bool = False,
        capability_override: str | None = None,
    ) -> LaunchSpec:
        if is_resume:
            raise ValueError("Copilot CLI resume is not supported by this provider increment.")
        self.validate_prompt(prompt)
        # Provenance gate (audit SEC-1/D-01): Copilot launches with a hardcoded
        # `--allow-all-tools --no-ask-user` and has no read-only tool mode wired
        # here, so it cannot honour the untrusted->read-only downgrade the other
        # providers apply. Fail closed rather than grant full, unattended tool
        # access to attacker-influenced (imported) input, unless the operator has
        # explicitly elevated this task. The sole exception is the dedicated
        # independent-review task type: its exact diff is already in the prompt,
        # and --available-tools= enforces a model-only launch with zero tools.
        model_only = task_type in agent_runner.MODEL_ONLY_TASK_TYPES
        if untrusted and not operator_elevated and not model_only:
            raise RuntimeError(
                "Copilot cannot run an untrusted (imported) task: it has no "
                "read-only tool mode, so it fails closed instead of granting "
                "--allow-all-tools to untrusted input (audit SEC-1/D-01). Elevate "
                "the task explicitly or run it with a provider that supports a "
                "read-only profile."
            )
        environment = dict(os.environ)
        _sensitive_environment_values(environment)
        availability = self.availability()
        if not availability.available or not availability.executable:
            raise RuntimeError(availability.message)

        argv = [
            availability.executable,
            "--output-format", "json",
            "--no-color",
            "--no-ask-user",
            "--no-auto-update",
            "--no-remote",
            "--no-remote-export",
            "--no-custom-instructions",
            "--disable-builtin-mcps",
            "-C", str(repository_path),
        ]
        if model_only:
            argv.append("--available-tools=")
        else:
            argv.append("--allow-all-tools")
        if model:
            argv.extend(["--model", model])

        audit = {
            "provider_id": self.id,
            "provider_version": availability.version,
            "non_interactive": True,
            "readiness": "assistant_turn_start",
            "result_evidence": "assistant_message_then_turn_end",
            "cancellation": "verified_process_group_sigterm_then_sigkill",
            **_prompt_audit(prompt, "stdin"),
        }
        return LaunchSpec(tuple(argv), environment, prompt, audit)

    @staticmethod
    def create_runtime(*, prompt: str, environment: dict[str, str]) -> ProviderRuntime:
        return CopilotRuntime(prompt, _sensitive_environment_values(environment))

    def parse_stdout_line(self, line: str) -> dict | None:
        return CopilotRuntime("").parse_stdout_line(line)

    @staticmethod
    def sanitize_stderr(line: str) -> str:
        runtime = CopilotRuntime("")
        runtime.feed_stderr(line)
        return "".join(runtime.flush_stderr())

    @staticmethod
    def classify_failure(*, exit_code: int, diagnostic_lines: list[str]) -> str | None:
        text = "\n".join(diagnostic_lines).lower()
        if any(token in text for token in ("quota", "usage limit", "spend limit", "rate limit", "ai credits")):
            return "quota_limit"
        if any(token in text for token in ("not logged in", "authentication", "unauthorized", "api key", "login")):
            return "authentication_failed"
        if any(token in text for token in ("network", "connection", "timeout", "econnrefused")):
            return "network_error"
        return "provider_exit_nonzero" if exit_code else None


_PROVIDERS: dict[str, ExecutionProvider] = {
    CLAUDE_ID: ClaudeProvider(),
    CODEX_ID: CodexProvider(),
    OLLAMA_ID: OllamaProvider(),
    COPILOT_ID: CopilotProvider(),
}


def get_provider(provider_id: str) -> ExecutionProvider:
    try:
        return _PROVIDERS[provider_id]
    except KeyError as exc:
        raise ValueError(f"Unknown execution provider: {provider_id!r}") from exc


def provider_ids() -> tuple[str, ...]:
    return tuple(_PROVIDERS)


def audit_json(metadata: dict[str, object]) -> str:
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
