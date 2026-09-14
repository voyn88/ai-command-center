"""Negative tests for the redaction/allowlist boundary.

A hostile-or-buggy projection artifact stuffed with secrets, absolute paths,
raw prompts and unknown fields must come out the other side clean — and the
body must still pass the *client's* own prohibited scan, otherwise every
installed client rejects the snapshot wholesale.
"""

from __future__ import annotations

import json
import logging

from native_gateway.redaction import (
    REDACTED,
    REPO_ROOT,
    PathRedactingFilter,
    find_violation,
    relativize_filepaths,
    sanitize_tree,
)

from .conftest import auth_headers, fresh_sample

SECRET_VALUES = [
    "password=hunter2",
    "Authorization: Bearer abc.def.ghi",
    "postgres://aios:hunter2@db.internal:5432/aios",
    "postgresql://aios@db/aios",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "ssh-rsa AAAAB3NzaC1yc2E owner@host",
    "ssh-ed25519 AAAAC3Nza owner@host",
    "/Users/dmitrijcernikov/secrets/id_rsa",
    "C:\\Users\\owner\\secrets",
    "the raw prompt was: ...",
    "ghp_0123456789abcdef",
    "github_pat_0123456789",
    "api_key=sk-abcdefgh12345678",
]


def test_every_secret_shape_is_detected():
    for value in SECRET_VALUES:
        assert find_violation(value) is not None, value


def test_sanitize_tree_replaces_whole_values():
    tree = {"a": SECRET_VALUES[0], "b": ["ok", SECRET_VALUES[2]], "c": {"d": "clean"}}
    out = sanitize_tree(tree)
    assert out["a"] == REDACTED
    assert out["b"] == ["ok", REDACTED]
    assert out["c"]["d"] == "clean"


def test_hostile_projection_is_fully_redacted(client, device_token, projection_path):
    data = fresh_sample(projection_path)
    data["tasks"][0]["title"] = "Rotate password for postgres://aios@db/aios"
    data["tasks"][0]["blocker"] = "ssh-rsa AAAAB3NzaC1yc2E leaked"
    data["tasks"][0]["evidence"]["head_sha"] = "/Users/dmitrijcernikov/Projects/x"
    data["events"][0]["summary"] = "raw prompt: do the thing"
    data["dialogs"][0]["last_summary"] = "-----BEGIN OPENSSH PRIVATE KEY-----"
    # Forbidden/unknown fields must be dropped by the allowlist, not serialized.
    data["tasks"][0]["ssh_command"] = "ssh voynadmin@control-01"
    data["pg_dsn"] = "postgres://aios:hunter2@db/aios"
    data["raw_log"] = "Traceback (most recent call last): ..."
    projection_path.write_text(json.dumps(data), encoding="utf-8")

    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    assert response.status_code == 200
    body = response.json()
    assert body["tasks"][0]["title"] == REDACTED
    assert body["tasks"][0]["blocker"] == REDACTED
    assert body["tasks"][0]["evidence"]["headSHA"] == REDACTED
    assert body["events"][0]["summary"] == REDACTED
    assert "ssh_command" not in response.text
    assert "pg_dsn" not in response.text
    assert "Traceback" not in response.text

    lowered = response.text.lower()
    for needle in (
        "password",
        "postgres://",
        "ssh-rsa",
        "private_key",
        "prompt",
        "/users/",
        "bearer ",
    ):
        assert needle not in lowered, needle


def test_hostile_dialog_summary_redacted_on_dialogs_route(
    client, device_token, projection_path
):
    data = fresh_sample(projection_path)
    data["dialogs"][0]["last_summary"] = "token=ghp_0123456789abcdef"
    projection_path.write_text(json.dumps(data), encoding="utf-8")
    response = client.get("/v1/dialogs", headers=auth_headers(device_token))
    assert response.status_code == 200
    assert response.json()["items"][0]["lastSummary"] == REDACTED


def test_clean_content_is_not_redacted(client, device_token):
    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    body = response.json()
    assert body["tasks"][0]["title"] == "Example delivery"
    assert body["events"][0]["summary"] == "PR #42 opened"


def test_relativize_filepaths_rewrites_repo_paths_to_relative():
    in_repo = REPO_ROOT / "native_gateway" / "projection_producer.py"
    text = f'File "{in_repo}", line 312, in build_projection'
    out = relativize_filepaths(text)
    assert str(in_repo) not in out
    assert "native_gateway/projection_producer.py" in out


def test_relativize_filepaths_redacts_paths_outside_the_repo():
    text = 'File "/home/someone/.venv/lib/python3.12/site-packages/x.py", line 1'
    out = relativize_filepaths(text)
    assert "/home/someone" not in out
    assert REDACTED in out


def test_path_redacting_filter_scrubs_pathname_message_and_traceback(caplog):
    logger = logging.getLogger("native_gateway.redaction.test")
    logger.addFilter(PathRedactingFilter())
    db_path = REPO_ROOT.parent / "outside-repo" / "runtime.db"

    with caplog.at_level(logging.WARNING, logger=logger.name):
        try:
            raise RuntimeError("schema mismatch")
        except RuntimeError:
            logger.warning("Run journal unavailable (db=%s)", db_path, exc_info=True)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert str(db_path) not in record.getMessage()
    assert REDACTED in record.getMessage()
    assert not record.pathname.startswith("/")
    assert record.exc_info is not None
