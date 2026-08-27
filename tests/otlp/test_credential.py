"""The credential file: what it must be, and that rotation is picked up.

Every assertion here is about a way the OTLP bearer token could be stored or
handled unsafely. The rotation tests exist because this fleet really does
rotate worker credentials on a timer (``voyn-aicc-credential-rotation.service``), so a
token captured once at startup is a slow, silent outage waiting to happen.
"""

from __future__ import annotations

import os
import stat

import pytest

from command_center.otlp.credential import (
    MAX_TOKEN_BYTES,
    Credential,
    CredentialError,
)

TOKEN = "s3cr3t-otlp-token-value"


def write_token(path, value: str = TOKEN, mode: int = 0o600):
    path.write_text(value, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_a_well_formed_file_loads(tmp_path):
    path = write_token(tmp_path / "otlp.token")
    assert Credential.from_path(path).token == TOKEN


def test_a_trailing_newline_is_stripped_not_rejected(tmp_path):
    """`echo "$TOKEN" > file` is how an operator will create this file."""
    path = write_token(tmp_path / "otlp.token", TOKEN + "\n")
    assert Credential.from_path(path).token == TOKEN


def test_a_group_or_world_readable_file_refuses(tmp_path):
    path = write_token(tmp_path / "otlp.token", mode=0o644)
    with pytest.raises(CredentialError, match="readable by group or others"):
        Credential.from_path(path)


def test_mode_0400_is_accepted_because_systemd_credentials_use_it(tmp_path):
    path = write_token(tmp_path / "otlp.token", mode=0o400)
    assert Credential.from_path(path).token == TOKEN


def test_a_missing_file_refuses(tmp_path):
    with pytest.raises(CredentialError, match="cannot be read"):
        Credential.from_path(tmp_path / "absent")


def test_a_directory_refuses(tmp_path):
    with pytest.raises(CredentialError, match="regular file"):
        Credential.from_path(tmp_path)


def test_an_empty_file_refuses(tmp_path):
    """A half-finished rotation leaves this; `Bearer ` is not anonymous."""
    path = write_token(tmp_path / "otlp.token", "")
    with pytest.raises(CredentialError, match="empty"):
        Credential.from_path(path)


def test_a_whitespace_only_file_refuses(tmp_path):
    path = write_token(tmp_path / "otlp.token", "   \n\t ")
    with pytest.raises(CredentialError, match="empty"):
        Credential.from_path(path)


def test_an_interior_newline_refuses_because_it_is_header_injection(tmp_path):
    path = write_token(tmp_path / "otlp.token", "good\r\nX-Injected: yes")
    with pytest.raises(CredentialError, match="header value"):
        Credential.from_path(path)


def test_an_oversized_file_refuses(tmp_path):
    path = write_token(tmp_path / "otlp.token", "a" * (MAX_TOKEN_BYTES + 1))
    with pytest.raises(CredentialError, match="over the"):
        Credential.from_path(path)


def test_a_binary_file_refuses(tmp_path):
    path = tmp_path / "otlp.token"
    path.write_bytes(b"\xff\xfe\x00binary")
    os.chmod(path, 0o600)
    with pytest.raises(CredentialError, match="not UTF-8"):
        Credential.from_path(path)


def test_no_error_message_ever_quotes_the_token(tmp_path):
    """Every refusal above reaches a log. None of them may carry the secret."""
    path = write_token(tmp_path / "otlp.token", TOKEN, mode=0o644)
    with pytest.raises(CredentialError) as caught:
        Credential.from_path(path)
    assert TOKEN not in str(caught.value)


def test_repr_does_not_expose_the_token(tmp_path):
    credential = Credential.from_path(write_token(tmp_path / "otlp.token"))
    assert TOKEN not in repr(credential)
    assert "otlp.token" in repr(credential)


def test_redact_removes_the_token_from_borrowed_text(tmp_path):
    credential = Credential.from_path(write_token(tmp_path / "otlp.token"))
    echoed = f'{{"error":"bad auth","seen":"Bearer {TOKEN}"}}'
    assert TOKEN not in credential.redact(echoed)
    assert "<redacted>" in credential.redact(echoed)


def test_rotation_is_observed_without_a_restart(tmp_path):
    """An atomic replace under the same path must change what is presented."""
    path = write_token(tmp_path / "otlp.token", "first-token-value")
    credential = Credential.from_path(path)
    assert credential.token == "first-token-value"

    replacement = tmp_path / "otlp.token.new"
    write_token(replacement, "second-token-value")
    os.replace(replacement, path)

    assert credential.token == "second-token-value"


def test_rotation_is_observed_even_when_the_size_is_unchanged(tmp_path):
    """Same length, replaced in place: device+inode+mtime still differ."""
    path = write_token(tmp_path / "otlp.token", "aaaaaaaaaaaaaaaa")
    credential = Credential.from_path(path)
    assert credential.token == "aaaaaaaaaaaaaaaa"

    replacement = tmp_path / "otlp.token.new"
    write_token(replacement, "bbbbbbbbbbbbbbbb")
    os.replace(replacement, path)

    assert credential.token == "bbbbbbbbbbbbbbbb"


def test_reload_rereads_unconditionally(tmp_path):
    path = write_token(tmp_path / "otlp.token", "first-token-value")
    credential = Credential.from_path(path)
    write_token(path, "second-token-value")
    assert credential.reload() == "second-token-value"


def test_a_rotation_that_leaves_an_unreadable_file_is_raised_not_hidden(tmp_path):
    """Serving the last good token quietly would hide a broken rotation."""
    path = write_token(tmp_path / "otlp.token")
    credential = Credential.from_path(path)
    assert credential.token == TOKEN

    path.unlink()
    with pytest.raises(CredentialError):
        _ = credential.token


def test_a_rotation_to_a_world_readable_file_refuses(tmp_path):
    """Validation is re-applied on re-read, not only at startup."""
    path = write_token(tmp_path / "otlp.token")
    credential = Credential.from_path(path)
    assert credential.token == TOKEN

    replacement = tmp_path / "otlp.token.new"
    write_token(replacement, "rotated-into-a-bad-mode", mode=0o644)
    os.replace(replacement, path)

    with pytest.raises(CredentialError, match="readable by group or others"):
        _ = credential.token


def test_an_in_place_same_length_rewrite_within_one_tick_needs_reload(tmp_path):
    """The documented limit of the stat check, pinned rather than papered over.

    mtime is stamped at tick granularity, so an in-place rewrite of identical
    length inside one tick leaves ``(dev, ino, mtime_ns, size)`` unchanged and
    the cached token stands. This is exactly why the transport forces
    :meth:`Credential.reload` on a 401 instead of trusting the cache: the
    stale token costs at most one rejected export.

    If a future kernel or filesystem makes the stat check sharp, the first
    assertion here starts failing -- which is the signal to delete it, not a
    regression.
    """
    path = write_token(tmp_path / "otlp.token", "aaaaaaaaaaaaaaaa")
    credential = Credential.from_path(path)
    assert credential.token == "aaaaaaaaaaaaaaaa"

    before = path.stat()
    path.write_text("bbbbbbbbbbbbbbbb", encoding="utf-8")
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        pytest.skip("this filesystem timestamped the two writes distinctly")

    assert credential.token == "aaaaaaaaaaaaaaaa", "the stat check cannot see it"
    assert credential.reload() == "bbbbbbbbbbbbbbbb", "reload is the backstop"


@pytest.mark.skipif(os.name != "posix", reason="mode bits are meaningless here")
def test_the_mode_check_is_the_posix_permission_bits(tmp_path):
    """Pins which bits are consulted, so a later refactor cannot widen it."""
    path = write_token(tmp_path / "otlp.token", mode=0o640)
    info = path.stat()
    assert stat.S_IMODE(info.st_mode) & 0o077
    with pytest.raises(CredentialError):
        Credential.from_path(path)
