from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Mac/worker transport requires POSIX process groups and permissions",
)


def module(name):
    path = Path(__file__).parents[2] / "ops" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def transcript(tools=None, mcp=None, result=None):
    return "\n".join(
        json.dumps(x)
        for x in [
            {
                "type": "system",
                "subtype": "init",
                "tools": [] if tools is None else tools,
                "mcp_servers": [] if mcp is None else mcp,
                "model": "subscription-model",
            },
            {
                "type": "result",
                "is_error": False,
                "result": "VERDICT: ACCEPT\nHEAD_SHA: " + "a" * 40,
            }
            if result is None
            else result,
        ]
    ).encode()


def test_mac_command_disables_customizations_tools_and_persistence():
    server = module("aicc_claude_mac_server")
    args = server.command("/official/claude")
    assert args[0] == "/official/claude"
    for flag in [
        "--safe-mode",
        "--restricted",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
    ]:
        assert flag in args
    assert args[args.index("--tools") + 1] == ""
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--dangerously-skip-permissions" not in args


def test_subscription_environment_never_uses_api_keys_or_exports_transport_token(
    monkeypatch,
):
    server = module("aicc_claude_mac_server")
    for key in [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "AICC_CLAUDE_MAC_TOKEN",
    ]:
        monkeypatch.setenv(key, "secret")
    env = server.subscription_environment()
    assert not any("secret" == value for value in env.values())
    assert env["DISABLE_AUTOUPDATER"] == "1"


def test_only_attested_zero_tool_results_are_returned():
    server = module("aicc_claude_mac_server")
    result = server.parse_output(transcript())
    assert result["zero_tools"] and not result["result"]["is_error"]
    with pytest.raises(ValueError):
        server.parse_output(transcript(tools=["Read"]))
    with pytest.raises(ValueError):
        server.parse_output(transcript(mcp=[{"name": "unexpected"}]))
    with pytest.raises(ValueError):
        server.parse_output(b'{"type":"result","is_error":false,"result":"ACCEPT"}')
    with pytest.raises(ValueError):
        server.parse_output(
            transcript()
            + b'\n{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"}]}}'
        )


def test_private_transport_token_rejects_symlinks_and_broad_modes(tmp_path):
    server = module("aicc_claude_mac_server")
    token = tmp_path / "token"
    token.write_text("a" * 64)
    token.chmod(0o600)
    assert server.load_token(token) == "a" * 64
    token.chmod(0o644)
    with pytest.raises(ValueError):
        server.load_token(token)
    token.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(ValueError):
        server.load_token(link)


def test_mac_driver_never_accepts_caller_selected_endpoint(monkeypatch, capsys):
    driver = module("aicc_claude_mac_agent")
    monkeypatch.setattr("sys.argv", ["client", "--", "text"])
    monkeypatch.delenv("AICC_CLAUDE_MAC_TOKEN", raising=False)
    assert driver.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["is_error"] and result["terminal_reason"] == "api_error"
    assert driver.ENDPOINT == "https://127.0.0.1:17443/v1/review"


def test_tls_endpoint_rejects_unauthenticated_requests_and_pins_server(
    tmp_path, monkeypatch
):
    import ssl
    import subprocess
    import threading
    import urllib.error
    import urllib.request

    server_module = module("aicc_claude_mac_server")
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    conf = tmp_path / "tls.cnf"
    conf.write_text(
        "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=v3\n[dn]\nCN=test\n[v3]\nsubjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\n"
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-config",
            str(conf),
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    monkeypatch.setattr(
        server_module,
        "infer",
        lambda prompt, *args: {
            "v": 1,
            "zero_tools": True,
            "model": "test",
            "result": {"is_error": False, "result": prompt},
        },
    )
    service = server_module.Server("a" * 64, tmp_path, cert, key, port=0)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    url = "https://127.0.0.1:" + str(service.server_port) + "/v1/review"
    context = ssl.create_default_context(cafile=str(cert))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    body = json.dumps({"v": 1, "prompt": "text only"}).encode()
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            opener.open(urllib.request.Request(url, data=body), timeout=3)
        assert refused.value.code == 401
        request = urllib.request.Request(
            url, data=body, headers={"Authorization": "Bearer " + "a" * 64}
        )
        with opener.open(request, timeout=3) as response:
            assert json.load(response)["result"]["result"] == "text only"
        untrusted = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        with pytest.raises(urllib.error.URLError):
            untrusted.open(request, timeout=3)
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=3)


def test_cancellation_terminates_only_its_own_request_process(tmp_path, monkeypatch):
    import socket
    import subprocess
    import sys
    import threading
    import time

    server = module("aicc_claude_mac_server")
    monkeypatch.setattr(server, "verify_subscription", lambda *a: None)
    monkeypatch.setattr(
        server, "command", lambda: [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    caller, peer = socket.socketpair()
    failures = []

    def request():
        try:
            server.infer("text", caller, tmp_path)
        except ConnectionError:
            failures.append("cancelled")

    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    thread = threading.Thread(target=request)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.ACTIVE_CHILDREN and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.ACTIVE_CHILDREN
        own = list(server.ACTIVE_CHILDREN)
        peer.close()
        thread.join(timeout=5)
        assert not thread.is_alive() and failures == ["cancelled"]
        assert all(p.poll() is not None for p in own)
        assert unrelated.poll() is None
    finally:
        caller.close()
        peer.close()
        unrelated.terminate()
        unrelated.wait(timeout=5)
