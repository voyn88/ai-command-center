from __future__ import annotations

import json
from pathlib import Path

from command_center.ops import owner_notify


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"ok") -> None:
        self.status = status
        self._body = body

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _configure(monkeypatch, tmp_path: Path, *, url: str | None = "http://ntfy.test/t") -> Path:
    state = tmp_path / "owner-notify.json"
    if url is None:
        monkeypatch.delenv("AICC_OWNER_NTFY_URL", raising=False)
    else:
        monkeypatch.setenv("AICC_OWNER_NTFY_URL", url)
    monkeypatch.delenv("AICC_OWNER_NTFY_TOKEN", raising=False)
    monkeypatch.setenv("AICC_OWNER_NOTIFY_STATE", str(state))
    monkeypatch.setenv("AICC_OWNER_NOTIFY_COOLDOWN", "21600")
    return state


def test_notify_is_noop_without_url(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path, url=None)
    calls: list[object] = []

    def _boom(*_a, **_k):
        calls.append(1)
        raise AssertionError("urlopen must not run")

    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", _boom)
    assert owner_notify.notify("k", "t", "m") is False
    assert owner_notify.resolve("k", "t", "m") is False
    assert calls == []


def test_notify_posts_headers_and_truncates_body(monkeypatch, tmp_path) -> None:
    state = _configure(monkeypatch, tmp_path, url="https://ntfy.test/t")
    monkeypatch.setenv("AICC_OWNER_NTFY_TOKEN", "sekrit")
    captured: dict[str, object] = {}

    def _urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["headers"] = {k.title(): v for k, v in request.header_items()}
        captured["timeout"] = timeout
        captured["method"] = request.get_method()
        return _FakeResponse()

    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", _urlopen)
    long_message = "x" * 1500
    assert owner_notify.notify(
        "monitor:control-01:queue_stalled",
        "AICC: queue_stalled",
        long_message,
        priority="urgent",
        tags=("warning", "queue_stalled"),
        click_url="https://example.test/x",
    )

    assert captured["url"] == "https://ntfy.test/t"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 10
    assert captured["data"] == ("x" * 1000).encode("utf-8")
    headers = captured["headers"]
    assert headers["Title"] == "AICC: queue_stalled"
    assert headers["Priority"] == "urgent"
    assert headers["Tags"] == "warning,queue_stalled"
    assert headers["Click"] == "https://example.test/x"
    assert headers["Authorization"] == "Bearer sekrit"
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert "monitor:control-01:queue_stalled" in stored["open"]


def test_cooldown_dedupes_open_key(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path)
    posts: list[str] = []

    def _urlopen(request, timeout=0):
        posts.append(request.full_url)
        return _FakeResponse()

    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", _urlopen)
    assert owner_notify.notify("k1", "t", "first") is True
    assert owner_notify.notify("k1", "t", "again") is False
    assert posts == ["http://ntfy.test/t"]

    monkeypatch.setenv("AICC_OWNER_NOTIFY_COOLDOWN", "0")
    assert owner_notify.notify("k1", "t", "after-cooldown") is True
    assert len(posts) == 2


def test_resolve_only_when_open(monkeypatch, tmp_path) -> None:
    state = _configure(monkeypatch, tmp_path)
    posts: list[bytes] = []

    def _urlopen(request, timeout=0):
        posts.append(request.data or b"")
        return _FakeResponse()

    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", _urlopen)
    assert owner_notify.resolve("missing", "done", "cleared") is False
    assert posts == []

    assert owner_notify.notify("open-key", "t", "m") is True
    assert owner_notify.resolve("open-key", "done", "cleared") is True
    assert posts[-1] == b"cleared"
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert "open-key" not in stored["open"]
    assert owner_notify.resolve("open-key", "done", "again") is False


def test_corrupt_state_file_is_tolerated(monkeypatch, tmp_path) -> None:
    state = _configure(monkeypatch, tmp_path)
    state.write_text("{not-json", encoding="utf-8")

    def _urlopen(request, timeout=0):
        return _FakeResponse()

    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", _urlopen)
    assert owner_notify.notify("k", "t", "m") is True
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert stored["open"]["k"]["last_sent"] > 0


def test_network_errors_return_false_and_do_not_raise(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)

    def _urlopen(request, timeout=0):
        raise owner_notify.urllib.error.URLError("down")

    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", _urlopen)
    assert owner_notify.notify("k", "t", "m") is False
    err = capsys.readouterr().err
    assert "owner_notify: POST failed" in err


def test_non_http_scheme_is_not_a_channel(monkeypatch) -> None:
    from command_center.ops import owner_notify

    monkeypatch.setenv("AICC_OWNER_NTFY_URL", "file:///etc/passwd")
    assert owner_notify.notify("k", "t", "m") is False


def test_unicode_title_is_encoded_without_header_newlines(monkeypatch, tmp_path):
    from email.header import decode_header, make_header
    _configure(monkeypatch, tmp_path)
    captured = {}
    def send(request, timeout=0):
        captured.update({k.title(): v for k, v in request.header_items()})
        return _FakeResponse()
    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", send)
    assert owner_notify.notify("unicode", "Авария — очередь\r\nпроверка", "body")
    title = captured["Title"]
    title.encode("ascii")
    assert "\r" not in title and "\n" not in title
    assert str(make_header(decode_header(title))) == "Авария — очередь проверка"


def test_resolved_alert_reopens_without_flapping_push(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    sent = []
    def send(request, timeout=0):
        sent.append(request.data)
        return _FakeResponse()
    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", send)
    assert owner_notify.notify("flap", "alert", "bad")
    assert owner_notify.resolve("flap", "clear", "good")
    assert not owner_notify.notify("flap", "alert", "bad again")
    assert "flap" in owner_notify.open_keys()
    assert len(sent) == 2


def test_token_is_not_sent_over_plaintext(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AICC_OWNER_NTFY_TOKEN", "secret")
    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must refuse")))
    assert not owner_notify.notify("k", "title", "body")


def test_authorization_is_not_copied_to_redirect(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, url="https://ntfy.test/t")
    monkeypatch.setenv("AICC_OWNER_NTFY_TOKEN", "secret")
    def send(request, timeout=0):
        redirected = owner_notify.urllib.request.HTTPRedirectHandler().redirect_request(
            request, None, 302, "found", {}, "https://other.test/t"
        )
        assert not redirected.has_header("Authorization")
        return _FakeResponse()
    monkeypatch.setattr(owner_notify.urllib.request, "urlopen", send)
    assert owner_notify.notify("k", "title", "body")
