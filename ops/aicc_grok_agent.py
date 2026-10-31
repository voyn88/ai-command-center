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
SHELL = {'type': 'function', 'name': 'shell',
         'description': 'Run a bounded shell command in the assigned repository. Read files, edit, test and commit here. Never push or change remotes.',
         'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}},
                        'required': ['command'], 'additionalProperties': False}}


def completion(key: str, model: str, items: list, tools: bool = True) -> dict:
    body = json.dumps({'model': model, 'input': items, 'tools': [SHELL] if tools else [],
                       'max_output_tokens': 4096, 'store': False}).encode()
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


def run(prompt: str, model: str, tools: bool = True) -> int:
    if os.getcwd() != '/workspace' or os.geteuid() == 0:
        raise RuntimeError('Grok tools require the isolated /workspace agent unit')
    key = os.environ.get('XAI_API_KEY', '')
    if not key:
        raise RuntimeError('provider authentication unavailable')
    items = [{'role': 'system', 'content': 'Work only on the assigned task. Be economical: use targeted reads and tests. Never read model keys or print environment. Never push or alter remotes. The OS enforces workspace permissions. Finish with a truthful result and HEAD_SHA when code was committed.'},
             {'role': 'user', 'content': prompt}]
    spent = 0
    for _ in range(MAX_TURNS):
        response = completion(key, model, items, tools)
        spent += int(response.get('usage', {}).get('total_tokens', 0))
        output = response.get('output', [])
        if not isinstance(output, list) or not output:
            raise RuntimeError('provider returned no output')
        if response.get('status') != 'completed':
            raise RuntimeError('provider response incomplete')
        items.extend(output)
        calls = [item for item in output if item.get('type') == 'function_call']
        if calls and not tools:
            raise RuntimeError('tools are disabled for this review')
        if not calls:
            text = '\n'.join(part.get('text', '') for item in output
                if item.get('type') == 'message' for part in item.get('content', [])
                if part.get('type') == 'output_text')
            if not text.strip():
                raise RuntimeError('provider returned no final report')
            print(text.replace(key, '[REDACTED]'))
            print(f'AICC_USAGE: total_tokens={spent}', file=sys.stderr)
            return 0
        if spent >= MAX_TOTAL_TOKENS or len(calls) > 4:
            raise RuntimeError('agent budget exhausted')
        for call in calls:
            if call.get('name') != 'shell':
                raise RuntimeError('unsupported tool')
            items.append({'type': 'function_call_output', 'call_id': call['call_id'],
                          'output': shell(call['arguments']).replace(key, '[REDACTED]')})
    raise RuntimeError('agent turn budget exhausted')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='grok-4.3')
    parser.add_argument('--no-tools', action='store_true')
    parser.add_argument('prompt')
    args = parser.parse_args()
    try:
        return run(args.prompt, args.model, tools=not args.no_tools)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary must redact unexpected errors
        # Error type/detail only for our own safe exceptions, never raw payloads.
        print(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
