#!/usr/bin/python3
"""Bounded xAI Responses tool driver, launched only inside the AICC broker.

The broker supplies the dynamic UID, exact workspace mount, read-only profile,
timeout/cgroup teardown and model-only key. This driver has no publisher API.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

MAX_TURNS = 12
MAX_TOTAL_TOKENS = 60000
MAX_TOOLS_PER_TURN = 4
MAX_IDENTICAL_TOOL_REPEATS = 3
MAX_OUTPUT_TOKENS = 4096
# Official xAI default for grok-4.6 is reasoning high; pin low for economy/latency.
GROK_46_REASONING_EFFORT = 'low'

SHELL = {'type': 'function', 'name': 'shell',
         'description': 'Run a bounded shell command in the assigned repository. Read files, edit, test and commit here. Never push or change remotes.',
         'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}},
                        'required': ['command'], 'additionalProperties': False}}

# Structured stop reasons (stderr AICC_STOP). Never reuse one label for different budgets.
STOP_COMPLETED = 'completed'
STOP_TOKEN_BUDGET = 'token_budget'
STOP_TURN_BUDGET = 'turn_budget'
STOP_NO_PROGRESS = 'no_progress'
STOP_USAGE_UNKNOWN = 'usage_unknown'
STOP_PROVIDER_INCOMPLETE = 'provider_incomplete'
STOP_RUN_ERROR = 'run_error'

# Provider/network/shell failures that must not erase a local partial after progress.
_TRANSPORT_ERRORS = (RuntimeError, OSError, TimeoutError, urllib.error.URLError)


def completion(key: str, model: str, items: list, tools: bool = True) -> dict:
    payload = {
        'model': model,
        'input': items,
        'tools': [SHELL] if tools else [],
        'max_output_tokens': MAX_OUTPUT_TOKENS,
        'store': False,
    }
    # grok-4.6 defaults to reasoning high; override only that model to low.
    if model == 'grok-4.6':
        payload['reasoning'] = {'effort': GROK_46_REASONING_EFFORT}
    body = json.dumps(payload).encode()
    req = urllib.request.Request('https://api.x.ai/v1/responses', data=body,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                 'User-Agent': 'aicc-grok-agent/1'})
    try:
        # Request URL is a fixed HTTPS provider endpoint, never caller supplied.
        with urllib.request.urlopen(req, timeout=90) as response:  # nosec B310
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Never echo provider response bodies (they can contain submitted data).
        raise RuntimeError(f'provider HTTP {exc.code}') from None


def shell(arguments: str) -> str:
    data = json.loads(arguments)
    command = data.get('command')
    if not isinstance(command, str) or not command.strip() or len(command) > 12000:
        raise ValueError('invalid shell command')
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(['/bin/bash', '-lc', command], stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, timeout=45, check=False)
            code = result.returncode
        except subprocess.TimeoutExpired:
            # The enclosing unit owns descendant cancellation at run end.
            raise RuntimeError('shell timed out; stop this run') from None
        output.seek(0)
        text = output.read(16000).decode('utf-8', 'replace')
    return json.dumps({'exit_code': code, 'output': text})


def _message_text(output: list) -> str:
    return '\n'.join(part.get('text', '') for item in output
        if item.get('type') == 'message' for part in item.get('content', [])
        if part.get('type') == 'output_text')


def _parse_usage_tokens(response: dict) -> int | None:
    """Return provider total_tokens when present and parseable; else None.

    Missing usage is not treated as zero. Post-response accounting is a driver
    guard only — not a claim of a strict API monetary cap.
    """
    usage = response.get('usage')
    if not isinstance(usage, dict):
        return None
    raw = usage.get('total_tokens')
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _call_signature(call: dict) -> str:
    return f"{call.get('name')}\n{call.get('arguments')}"


def _tool_refusal_output(reason: str, limit: int) -> str:
    return json.dumps({
        'error': 'tool_budget_refused',
        'reason': reason,
        'limit': limit,
    }, sort_keys=True)


def _append_call_refusals(
    items: list,
    calls: list,
    key: str,
    *,
    reason: str,
    limit: int,
) -> int:
    """Append protocol-valid outputs for every pending function_call. Returns count."""
    refused = 0
    for call in calls:
        call_id = call.get('call_id')
        if not isinstance(call_id, str) or not call_id:
            raise RuntimeError('function_call missing call_id')
        items.append({
            'type': 'function_call_output',
            'call_id': call_id,
            'output': _redact(_tool_refusal_output(reason, limit), key),
        })
        refused += 1
    return refused


def _redact(text: str, key: str) -> str:
    return text.replace(key, '[REDACTED]') if key else text


def _emit_usage_and_stop(
    *,
    reason: str,
    turns: int,
    tools_executed: int,
    tools_refused: int,
    spent: int,
    usage_unknown: bool,
    identical_streak: int,
    failure_class: str = '',
) -> None:
    # Diagnostics only — never secrets, never provider bodies.
    failure_part = f' failure_class={failure_class}' if failure_class else ''
    print(
        f'AICC_STOP: reason={reason} turns={turns} tools_executed={tools_executed} '
        f'tools_refused={tools_refused} usage_tokens={spent if not usage_unknown else "unknown"} '
        f'usage_unknown={"true" if usage_unknown else "false"} '
        f'identical_streak={identical_streak} '
        f'max_turns={MAX_TURNS} max_total_tokens={MAX_TOTAL_TOKENS} '
        f'max_tools_per_turn={MAX_TOOLS_PER_TURN}'
        f'{failure_part}',
        file=sys.stderr,
    )
    print(
        f'AICC_USAGE: total_tokens={spent if not usage_unknown else "unknown"} '
        f'usage_unknown={"true" if usage_unknown else "false"} turns={turns} '
        f'tools_executed={tools_executed} tools_refused={tools_refused}',
        file=sys.stderr,
    )


def _local_partial_report(
    *,
    reason: str,
    turns: int,
    tools_executed: int,
    tools_refused: int,
    spent: int,
    usage_unknown: bool,
    tool_trace: list[str],
    failure_class: str = '',
) -> str:
    lines = [
        'AICC_PARTIAL_RESULT',
        f'stop_reason: {reason}',
        'status: incomplete_budget_or_guard_stop',
        f'turns: {turns}',
        f'tools_executed: {tools_executed}',
        f'tools_refused: {tools_refused}',
        f'usage_tokens: {"unknown" if usage_unknown else spent}',
        f'usage_unknown: {"true" if usage_unknown else "false"}',
        (
            'note: Driver stopped before a completed final model report. '
            'Executed tool side effects (if any) remain in the workspace. '
            'This is not a successful completion.'
        ),
        '',
        '## Tool trace (arguments truncated, secrets redacted)',
    ]
    if failure_class:
        lines.insert(3, f'failure_class: {failure_class}')
    if tool_trace:
        lines.extend(f'{index}. {entry}' for index, entry in enumerate(tool_trace, start=1))
    else:
        lines.append('(no tools executed)')
    return '\n'.join(lines)


def _finish_partial(
    key: str,
    *,
    reason: str,
    turns: int,
    tools_executed: int,
    tools_refused: int,
    spent: int,
    usage_unknown: bool,
    identical_streak: int,
    tool_trace: list[str],
    failure_class: str = '',
) -> int:
    # Deterministic durable partial only — no optional extra model summary call.
    # (A prior reserve+summary path was not bounded by remaining budget once
    # max_output_tokens plus prompt could exceed the leftover.)
    local = _local_partial_report(
        reason=reason,
        turns=turns,
        tools_executed=tools_executed,
        tools_refused=tools_refused,
        spent=spent,
        usage_unknown=usage_unknown,
        tool_trace=tool_trace,
        failure_class=failure_class,
    )
    print(_redact(local, key))
    _emit_usage_and_stop(
        reason=reason,
        turns=turns,
        tools_executed=tools_executed,
        tools_refused=tools_refused,
        spent=spent,
        usage_unknown=usage_unknown,
        identical_streak=identical_streak,
        failure_class=failure_class,
    )
    # Non-zero: partial/budget/guard stops are never successful completion.
    return 1


def _has_progress(tools_executed: int, tool_trace: list[str], tools_refused: int) -> bool:
    return tools_executed > 0 or bool(tool_trace) or tools_refused > 0


def run(prompt: str, model: str, tools: bool = True) -> int:
    if os.getcwd() != '/workspace' or os.geteuid() == 0:
        raise RuntimeError('Grok tools require the isolated /workspace agent unit')
    key = os.environ.get('XAI_API_KEY', '')
    if not key:
        raise RuntimeError('provider authentication unavailable')
    items = [{'role': 'system', 'content': 'Work only on the assigned task. Be economical: use targeted reads and tests. Never read model keys or print environment. Never push or alter remotes. The OS enforces workspace permissions. Finish with a truthful result and HEAD_SHA when code was committed.'},
             {'role': 'user', 'content': prompt}]
    spent = 0
    usage_unknown = False
    tools_executed = 0
    tools_refused = 0
    tool_trace: list[str] = []
    identical_streak = 0
    last_signature: str | None = None
    turn = 0

    try:
        for turn in range(1, MAX_TURNS + 1):
            response = completion(key, model, items, tools)
            tokens = _parse_usage_tokens(response)
            if tokens is None:
                usage_unknown = True
            else:
                spent += tokens
            output = response.get('output', [])
            if not isinstance(output, list) or not output:
                if _has_progress(tools_executed, tool_trace, tools_refused):
                    return _finish_partial(
                        key,
                        reason=STOP_PROVIDER_INCOMPLETE,
                        turns=turn,
                        tools_executed=tools_executed,
                        tools_refused=tools_refused,
                        spent=spent,
                        usage_unknown=usage_unknown,
                        identical_streak=identical_streak,
                        tool_trace=tool_trace,
                        failure_class='empty_output',
                    )
                raise RuntimeError('provider returned no output')
            if response.get('status') != 'completed':
                if _has_progress(tools_executed, tool_trace, tools_refused):
                    return _finish_partial(
                        key,
                        reason=STOP_PROVIDER_INCOMPLETE,
                        turns=turn,
                        tools_executed=tools_executed,
                        tools_refused=tools_refused,
                        spent=spent,
                        usage_unknown=usage_unknown,
                        identical_streak=identical_streak,
                        tool_trace=tool_trace,
                        failure_class='incomplete_status',
                    )
                raise RuntimeError('provider response incomplete')
            items.extend(output)
            calls = [item for item in output if item.get('type') == 'function_call']
            if calls and not tools:
                raise RuntimeError('tools are disabled for this review')
            if not calls:
                text = _message_text(output)
                if not text.strip():
                    if _has_progress(tools_executed, tool_trace, tools_refused):
                        return _finish_partial(
                            key,
                            reason=STOP_PROVIDER_INCOMPLETE,
                            turns=turn,
                            tools_executed=tools_executed,
                            tools_refused=tools_refused,
                            spent=spent,
                            usage_unknown=usage_unknown,
                            identical_streak=identical_streak,
                            tool_trace=tool_trace,
                            failure_class='empty_final_report',
                        )
                    raise RuntimeError('provider returned no final report')
                print(_redact(text, key))
                _emit_usage_and_stop(
                    reason=STOP_COMPLETED,
                    turns=turn,
                    tools_executed=tools_executed,
                    tools_refused=tools_refused,
                    spent=spent,
                    usage_unknown=usage_unknown,
                    identical_streak=identical_streak,
                )
                return 0

            # After a tool-bearing response: decide whether more tools may run.
            if usage_unknown:
                tools_refused += _append_call_refusals(
                    items, calls, key,
                    reason='usage_unknown',
                    limit=0,
                )
                return _finish_partial(
                    key,
                    reason=STOP_USAGE_UNKNOWN,
                    turns=turn,
                    tools_executed=tools_executed,
                    tools_refused=tools_refused,
                    spent=spent,
                    usage_unknown=usage_unknown,
                    identical_streak=identical_streak,
                    tool_trace=tool_trace,
                )
            if spent >= MAX_TOTAL_TOKENS:
                tools_refused += _append_call_refusals(
                    items, calls, key,
                    reason='token_budget',
                    limit=MAX_TOTAL_TOKENS,
                )
                return _finish_partial(
                    key,
                    reason=STOP_TOKEN_BUDGET,
                    turns=turn,
                    tools_executed=tools_executed,
                    tools_refused=tools_refused,
                    spent=spent,
                    usage_unknown=usage_unknown,
                    identical_streak=identical_streak,
                    tool_trace=tool_trace,
                )

            for index, call in enumerate(calls):
                name = call.get('name')
                if name != 'shell':
                    raise RuntimeError('unsupported tool')
                call_id = call['call_id']
                arguments = call.get('arguments', '')
                signature = _call_signature(call)
                if index >= MAX_TOOLS_PER_TURN:
                    # Do not discard the whole batch: refuse excess with valid protocol outputs.
                    tools_refused += 1
                    items.append({
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': _redact(
                            _tool_refusal_output('max_tools_per_turn', MAX_TOOLS_PER_TURN),
                            key,
                        ),
                    })
                    continue

                if last_signature is not None and signature == last_signature:
                    identical_streak += 1
                else:
                    identical_streak = 1
                    last_signature = signature
                if identical_streak > MAX_IDENTICAL_TOOL_REPEATS:
                    # Keep protocol valid for this and any remaining calls in the batch.
                    for pending in calls[index:]:
                        tools_refused += 1
                        items.append({
                            'type': 'function_call_output',
                            'call_id': pending['call_id'],
                            'output': _redact(
                                _tool_refusal_output('no_progress', MAX_IDENTICAL_TOOL_REPEATS),
                                key,
                            ),
                        })
                    return _finish_partial(
                        key,
                        reason=STOP_NO_PROGRESS,
                        turns=turn,
                        tools_executed=tools_executed,
                        tools_refused=tools_refused,
                        spent=spent,
                        usage_unknown=usage_unknown,
                        identical_streak=identical_streak,
                        tool_trace=tool_trace,
                    )

                preview = arguments if isinstance(arguments, str) else str(arguments)
                if len(preview) > 200:
                    preview = preview[:200] + '…'
                tool_trace.append(_redact(f'shell {preview}', key))
                items.append({
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': _redact(shell(arguments), key),
                })
                tools_executed += 1

        return _finish_partial(
            key,
            reason=STOP_TURN_BUDGET,
            turns=MAX_TURNS,
            tools_executed=tools_executed,
            tools_refused=tools_refused,
            spent=spent,
            usage_unknown=usage_unknown,
            identical_streak=identical_streak,
            tool_trace=tool_trace,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except _TRANSPORT_ERRORS as exc:
        if _has_progress(tools_executed, tool_trace, tools_refused):
            return _finish_partial(
                key,
                reason=STOP_RUN_ERROR,
                turns=turn,
                tools_executed=tools_executed,
                tools_refused=tools_refused,
                spent=spent,
                usage_unknown=usage_unknown,
                identical_streak=identical_streak,
                tool_trace=tool_trace,
                failure_class=type(exc).__name__,
            )
        raise
    except Exception as exc:
        # Unexpected driver/shell errors after progress: honest partial, never success.
        # Do not retry. KeyboardInterrupt/SystemExit are not caught here.
        if _has_progress(tools_executed, tool_trace, tools_refused):
            return _finish_partial(
                key,
                reason=STOP_RUN_ERROR,
                turns=turn,
                tools_executed=tools_executed,
                tools_refused=tools_refused,
                spent=spent,
                usage_unknown=usage_unknown,
                identical_streak=identical_streak,
                tool_trace=tool_trace,
                failure_class=type(exc).__name__,
            )
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='grok-4.3')
    parser.add_argument('--no-tools', action='store_true')
    parser.add_argument('prompt')
    args = parser.parse_args()
    try:
        return run(args.prompt, args.model, tools=not args.no_tools)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 -- CLI boundary must redact unexpected errors
        # Error type/detail only for our own safe exceptions, never raw payloads.
        print(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
