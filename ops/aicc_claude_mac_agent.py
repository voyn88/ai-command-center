#!/usr/bin/python3
"""Zero-tool Claude subscription transport; invoked only by the isolated broker."""

from __future__ import annotations
import argparse
import base64
import ssl
import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://127.0.0.1:17443/v1/review"
MAX_BYTES = 131072


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    args = parser.parse_args()
    token = os.environ.get("AICC_CLAUDE_MAC_TOKEN", "")
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        return failure(503)
    if not args.prompt or len(args.prompt.encode()) > 65536:
        return failure(400)
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"v": 1, "prompt": args.prompt}).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        certificate = base64.b64decode(
            os.environ.get("AICC_CLAUDE_MAC_CA_B64", ""), validate=True
        ).decode()
        if (
            not certificate.startswith("-----BEGIN CERTIFICATE-----")
            or len(certificate) > 16384
        ):
            return failure(503)
        context = ssl.create_default_context(cadata=certificate)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )
        with opener.open(request, timeout=200) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return failure(502)
        data = json.loads(raw)
        result = data.get("result")
        if (
            data.get("v") != 1
            or data.get("zero_tools") is not True
            or not isinstance(result, dict)
        ):
            return failure(502)
        if not isinstance(result.get("result"), str) or not isinstance(
            result.get("is_error"), bool
        ):
            return failure(502)
        result["aicc_transport"] = {
            "kind": "mac_claude_pro",
            "zero_tools": True,
            "model": data.get("model"),
        }
        print(json.dumps(result), flush=True)
        return 1 if result["is_error"] else 0
    except urllib.error.HTTPError as exc:
        return failure(exc.code)
    except (OSError, ValueError, urllib.error.URLError):
        return failure(503)


def failure(status: int) -> int:
    # Never echo HTTP bodies, tokens, prompts or raw provider diagnostics.
    print(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": status,
                "terminal_reason": "api_error",
                "result": "Claude Mac inference transport unavailable",
            }
        ),
        flush=True,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
