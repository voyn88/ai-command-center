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
    PATH_REDACTED,
    REDACTED,
    REPO_ROOT,
    PathRedactingFilter,
    find_violation,
    install_path_redaction,
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


# --------------------------------------------------------------------------
# Log-path redaction: PathRedactingFilter / relativize_filepaths.
#
# The prior version of this filter only matched paths beginning with a fixed
# allowlist of root directory names (Users/home/var/etc/opt/srv/root/private/
# tmp). That silently no-ops for any deployment rooted elsewhere — Docker's
# `WORKDIR /app`, `/usr/src/app`, `/workspace`, `/code`, `/nix/store`, etc.
# These tests specifically exercise roots *outside* that old fixed list to
# guard against regressing back to an allowlist-by-root-name approach.
# --------------------------------------------------------------------------


def test_in_repo_path_is_relativized_not_redacted():
    in_repo = REPO_ROOT / "native_gateway" / "redaction.py"
    out = relativize_filepaths(str(in_repo))
    assert out == "native_gateway/redaction.py"
    assert PATH_REDACTED not in out


def test_docker_workdir_app_path_is_redacted_not_leaked():
    # /app is a conventional Docker WORKDIR, not in the old fixed root list.
    text = 'File "/app/native_gateway/foo.py", line 42, in handler'
    out = relativize_filepaths(text)
    assert "/app/" not in out
    assert PATH_REDACTED in out


def test_usr_src_app_path_is_redacted_not_leaked():
    text = "loaded config from /usr/src/app/config/settings.yaml"
    out = relativize_filepaths(text)
    assert "/usr/src/app" not in out
    assert PATH_REDACTED in out


def test_workspace_path_is_redacted_not_leaked():
    text = "resolved module at /workspace/native_gateway/app.py"
    out = relativize_filepaths(text)
    assert "/workspace" not in out
    assert PATH_REDACTED in out


def test_legacy_allowlisted_roots_still_redacted_when_outside_repo():
    # /home/someone/... is outside this repo checkout, so even though it
    # matches the *old* fixed-root list, it must still be fully redacted
    # (not merely left alone) because it isn't inside REPO_ROOT.
    text = "loaded venv from /home/someone/.venv/lib/python3.11/site-packages"
    out = relativize_filepaths(text)
    assert "/home/someone" not in out
    assert PATH_REDACTED in out


def test_windows_path_is_redacted():
    text = r"cache dir: C:\Users\owner\AppData\Local\aicc"
    out = relativize_filepaths(text)
    assert "C:\\Users" not in out
    assert PATH_REDACTED in out


def test_non_path_slash_text_is_left_alone():
    text = "throughput improved and/or latency dropped 24/7"
    assert relativize_filepaths(text) == text


def test_path_redacting_filter_scrubs_pathname_message_and_exc_text():
    logger = logging.getLogger("test.path.redaction")
    logger.filters = []
    path_filter = install_path_redaction(logger)
    try:
        record = logger.makeRecord(
            logger.name,
            logging.ERROR,
            "/usr/src/app/native_gateway/foo.py",
            10,
            "boom near /app/secrets/config.yaml",
            (),
            None,
        )
        assert path_filter.filter(record) is True
        assert "/usr/src/app" not in record.pathname
        assert record.pathname == PATH_REDACTED
        assert "/app/secrets" not in record.getMessage()
    finally:
        logger.filters = []


def test_path_redacting_filter_scrubs_exception_traceback():
    logger = logging.getLogger("test.path.redaction.exc")
    logger.filters = []
    path_filter = install_path_redaction(logger)
    try:
        try:
            raise RuntimeError("nope")
        except RuntimeError:
            import sys

            exc_info = sys.exc_info()
        record = logger.makeRecord(
            logger.name,
            logging.ERROR,
            "/usr/src/app/native_gateway/foo.py",
            10,
            "unhandled",
            (),
            exc_info,
        )
        assert path_filter.filter(record) is True
        assert record.exc_text is not None
        assert "/usr/src/app" not in record.exc_text
    finally:
        logger.filters = []


def test_install_path_redaction_is_idempotent():
    logger = logging.getLogger("test.path.redaction.idempotent")
    logger.filters = []
    first = install_path_redaction(logger)
    second = install_path_redaction(logger)
    assert first is second
    assert sum(isinstance(f, PathRedactingFilter) for f in logger.filters) == 1
    logger.filters = []

