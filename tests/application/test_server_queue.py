import json

import pytest

from command_center.application import server_queue as sq


def test_disabled_client_refuses_to_ask_the_question():
    with pytest.raises(sq.ServerQueueDisabledError):
        sq.DisabledServerQueueClient().list_items()


def test_factory_requires_complete_https_allowlisted_configuration():
    common = {
        "AICC_SERVER_QUEUE_TOKEN": "secret",
        "AICC_SERVER_QUEUE_ALLOWED_HOSTS": "control-01.example",
    }
    assert isinstance(sq.create_server_queue_client({}), sq.DisabledServerQueueClient)
    assert isinstance(
        sq.create_server_queue_client(
            {**common, "AICC_SERVER_QUEUE_URL": "http://control-01.example"}
        ),
        sq.DisabledServerQueueClient,
    )
    assert isinstance(
        sq.create_server_queue_client(
            {**common, "AICC_SERVER_QUEUE_URL": "https://not-allowed.example"}
        ),
        sq.DisabledServerQueueClient,
    )


def test_factory_builds_the_http_client_when_fully_configured():
    client = sq.create_server_queue_client(
        {
            "AICC_SERVER_QUEUE_URL": "https://control-01.example",
            "AICC_SERVER_QUEUE_TOKEN": "secret",
            "AICC_SERVER_QUEUE_ALLOWED_HOSTS": "control-01.example",
        }
    )
    assert isinstance(client, sq.HTTPServerQueueClient)


def test_item_from_dict_mirrors_the_read_store_shape_verbatim():
    item = sq.ServerQueueItem.from_dict(
        {
            "work_item_id": "wi-1",
            "queue": "execution",
            "state": "ready",
            "task_id": "T-1",
            "repository_id": "repo-1",
            "priority": 5,
            "attempt_count": 0,
            "max_attempts": 3,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": None,
        }
    )
    assert item == sq.ServerQueueItem(
        work_item_id="wi-1",
        queue="execution",
        state="ready",
        task_id="T-1",
        repository_id="repo-1",
        priority=5,
        attempt_count=0,
        max_attempts=3,
        created_at="2026-01-01T00:00:00Z",
        updated_at=None,
    )


def test_http_client_raises_authentication_error_on_401(monkeypatch):
    monkeypatch.setattr(sq, "_http_get_json", lambda url, token, timeout: (401, b"{}"))
    client = sq.HTTPServerQueueClient(base_url="https://control-01.example", token="bad")
    with pytest.raises(sq.ServerQueueAuthenticationError):
        client.list_items()


def test_http_client_raises_remote_error_on_non_2xx(monkeypatch):
    monkeypatch.setattr(sq, "_http_get_json", lambda url, token, timeout: (500, b"{}"))
    client = sq.HTTPServerQueueClient(base_url="https://control-01.example", token="tok")
    with pytest.raises(sq.ServerQueueRemoteError):
        client.list_items()


def test_http_client_raises_remote_error_on_invalid_shape(monkeypatch):
    monkeypatch.setattr(
        sq, "_http_get_json", lambda url, token, timeout: (200, json.dumps({"nope": []}).encode())
    )
    client = sq.HTTPServerQueueClient(base_url="https://control-01.example", token="tok")
    with pytest.raises(sq.ServerQueueRemoteError):
        client.list_items()


def test_http_client_parses_items_on_success(monkeypatch):
    body = json.dumps(
        {
            "items": [
                {
                    "work_item_id": "wi-1",
                    "queue": "execution",
                    "state": "ready",
                    "task_id": "T-1",
                    "repository_id": None,
                    "priority": 1,
                    "attempt_count": 0,
                    "max_attempts": 3,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ]
        }
    ).encode()
    captured: dict = {}

    def fake_get(url, token, timeout):
        captured["url"] = url
        captured["token"] = token
        return 200, body

    monkeypatch.setattr(sq, "_http_get_json", fake_get)
    client = sq.HTTPServerQueueClient(base_url="https://control-01.example", token="tok")
    items = client.list_items(state="ready", limit=10)
    assert len(items) == 1
    assert items[0].work_item_id == "wi-1"
    assert captured["token"] == "tok"
    assert "state=ready" in captured["url"]
    assert "limit=10" in captured["url"]


def test_http_client_transport_failure_raises_timeout_error():
    client = sq.HTTPServerQueueClient(base_url="https://192.0.2.1", token="tok", timeout=0.01)
    with pytest.raises(sq.ServerQueueTimeoutError):
        client.list_items()
