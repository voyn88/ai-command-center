"""The bearer credential AICC presents to the OTLP ingest, read from a file.

WHY A FILE AND NOT AN ENVIRONMENT VARIABLE
==========================================
The rest of AICC's server configuration is environment-driven (see
``command_center/db/config.py``), and this module deliberately breaks that
symmetry for the one value that is a secret. An environment variable is
readable from ``/proc/<pid>/environ`` by anything running as the same user,
survives into ``systemctl show -p Environment``, and is copied verbatim into
every child process AICC spawns -- and AICC spawns agents. A token that grants
write access to the observability store must not be handed to every Claude
process the worker launches.

The deployment already has the right idiom and this module reuses it rather
than inventing one: ``voyn-aicc-worker.service`` carries
``LoadCredential=voyn_lease_pgpass:...`` and installs it at mode 0600 for the
lease client. ``AICC_OTLP_TOKEN_FILE`` names a file of exactly that shape, so
the operator's procedure for the OTLP token is the procedure they already run
for the database one.

WHY IT IS RE-READ RATHER THAN CAPTURED AT STARTUP
=================================================
Measured on the target host, not assumed: ``voyn-aicc-credential-rotation.service``
exists and rotates this fleet's worker credentials on a timer (SRV-03; see
``docs/operations/WORKER_CREDENTIAL_ROTATION.md``). A token captured once into
a long-lived exporter therefore goes stale on a schedule. The failure that produces is the worst-shaped one available here --
export starts answering 401, telemetry stops arriving, and nothing else
changes, so the first person to notice is whoever opens the dashboard during
the *next* incident and finds it empty.

So the credential is identified by its path, and :meth:`Credential.token`
re-reads whenever the file's identity or mtime changes. The cost is one
``os.stat`` per export batch. :meth:`Credential.reload` forces the read, which
is what the transport does on a 401 before deciding the rejection is real: a
rotation that lands between the stat and the request is a race the retry
closes, and treating that race as an outage would page someone for a file
write.

Rotation must publish the new token by atomic replace (write a temp file in
the same directory, ``os.replace`` onto the target). That is not only the
crash-safe way to do it, it is the only way this cache is guaranteed to notice
promptly, and the limit is measured rather than assumed: the kernel stamps
mtime at tick granularity, so two writes *into the same inode* within one tick
are indistinguishable by ``(mtime_ns, size)`` when the length is unchanged --
observed on this host, where two consecutive same-length writes reported the
identical ``st_mtime_ns``. An ``os.replace`` changes the inode and is detected
regardless. The residual case (in-place, same length, same tick) is not
detected by the stat check and is instead backstopped by
:meth:`Credential.reload`, which the transport calls on a 401: the stale token
survives at most one rejected export. That layering is why the retry in
:mod:`command_center.otlp.transport` is part of the design and not a nicety.

A re-read that fails is raised, not swallowed -- a credential file that has
become unreadable is a real fault, and the alternative (keep serving the last
good token, quietly) hides a broken rotation until the token expires.

WHAT IS VALIDATED, AND WHY EACH CHECK EARNS ITS PLACE
=====================================================
* **Mode.** Group- or world-readable refuses. A 0644 secret is not a secret,
  and the check costs nothing at the one moment an operator can still fix it.
  POSIX only: the mode bits ``os.stat`` reports on Windows describe nothing.
* **Control characters.** The token is interpolated into an HTTP header value.
  A newline in it is header injection -- and the most likely way to get one is
  ``echo "$TOKEN" > tokenfile``, which is why a *trailing* newline is stripped
  rather than rejected.
* **Length.** A bounded cap turns "the path points at a log file" into a clear
  error at startup instead of a multi-megabyte request header.
* **Emptiness.** An empty file is the state a half-finished rotation leaves
  behind; sending ``Authorization: Bearer `` would be indistinguishable from
  an authentication failure at the far end.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = ["CredentialError", "Credential", "MAX_TOKEN_BYTES"]

#: Generous for any real bearer token (a 512-bit key is 88 base64 characters),
#: small enough that a misdirected path fails loudly instead of being sent.
MAX_TOKEN_BYTES = 4096

#: Printable US-ASCII plus space: the safe subset of RFC 9110 field values.
#: Excludes CR, LF and NUL by construction, which is the point.
_MIN_ALLOWED_CHAR = 0x20
_MAX_ALLOWED_CHAR = 0x7E


class CredentialError(RuntimeError):
    """The token file does not describe a usable, safely-stored secret.

    Raised at load and again on any re-read that fails. Never carries the
    token itself: this exception is expected to reach a log.
    """


class Credential:
    """A bearer token identified by its path, re-read when the file changes.

    Construct with :meth:`from_path`, which reads and validates eagerly so a
    misconfigured deployment fails at startup rather than at the first export.
    """

    __slots__ = ("_path", "_token", "_fingerprint")

    def __init__(self, path: Path, token: str, fingerprint: tuple[int, ...]) -> None:
        self._path = path
        self._token = token
        self._fingerprint = fingerprint

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> "Credential":
        """Read, validate and return the credential at ``path``."""
        resolved = Path(path)
        token, fingerprint = _read(resolved)
        return cls(resolved, token, fingerprint)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def token(self) -> str:
        """The current token, re-reading the file if it has changed."""
        fingerprint = _fingerprint(self._path)
        if fingerprint != self._fingerprint:
            self._token, self._fingerprint = _read(self._path)
        return self._token

    def reload(self) -> str:
        """Re-read unconditionally and return the token.

        The transport calls this after a 401 so that a rotation landing
        between the stat and the request is retried rather than reported as
        an authentication failure.
        """
        self._token, self._fingerprint = _read(self._path)
        return self._token

    def redact(self, text: str) -> str:
        """Replace the token wherever it appears in ``text``.

        Applied to anything derived from a response before it is raised or
        logged: an ingest that echoes the request headers back in an error
        body would otherwise write the credential into AICC's own logs.
        """
        if not self._token:
            return text
        return text.replace(self._token, "<redacted>")

    def __repr__(self) -> str:
        # Never the token. A dataclass-generated repr is exactly how a secret
        # reaches a traceback, so this class is not a dataclass.
        return f"Credential(path={str(self._path)!r})"


def _fingerprint(path: Path) -> tuple[int, ...]:
    """Identity of the file's current contents, cheap enough to check per send.

    Device and inode catch an atomic replace whose mtime happens to match;
    size catches a truncation within the same nanosecond timestamp.
    """
    try:
        info = path.stat()
    except OSError as exc:
        raise CredentialError(
            f"{path}: the OTLP credential file cannot be read ({exc.strerror})."
        ) from exc
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def _read(path: Path) -> tuple[str, tuple[int, ...]]:
    """Return the validated token and the fingerprint it was read at.

    The fingerprint is taken *before* the read: if the file is replaced
    between the two, the stale fingerprint means the next access re-reads.
    Taking it afterwards would record the new file's identity against the old
    file's contents and pin the stale token indefinitely.
    """
    fingerprint = _fingerprint(path)

    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise CredentialError(f"{path}: the OTLP credential must be a regular file.")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise CredentialError(
            f"{path}: the OTLP credential is readable by group or others "
            f"(mode {stat.S_IMODE(info.st_mode):04o}); use 0600 (or 0400 for a "
            "systemd LoadCredential file)."
        )
    if info.st_size > MAX_TOKEN_BYTES:
        raise CredentialError(
            f"{path}: the OTLP credential is {info.st_size} bytes, over the "
            f"{MAX_TOKEN_BYTES}-byte limit. This path is almost certainly not a "
            "token file."
        )

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CredentialError(
            f"{path}: the OTLP credential file cannot be read ({exc.strerror})."
        ) from exc

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialError(
            f"{path}: the OTLP credential is not UTF-8 text."
        ) from exc

    return _validate(text, path), fingerprint


def _validate(text: str, path: Path) -> str:
    """Normalize and check a token. Error messages never quote its value."""
    # Only the surrounding whitespace: `echo` appends a newline, and refusing
    # that would make the obvious way to create the file the wrong way.
    token = text.strip()
    if not token:
        raise CredentialError(
            f"{path}: the OTLP credential file is empty. An empty bearer token "
            "is not an anonymous request, it is an unauthenticated one."
        )
    for index, char in enumerate(token):
        if not _MIN_ALLOWED_CHAR <= ord(char) <= _MAX_ALLOWED_CHAR:
            raise CredentialError(
                f"{path}: the OTLP credential contains a character that cannot "
                f"appear in an HTTP header value (offset {index}, code point "
                f"U+{ord(char):04X}). Interior newlines in particular would be "
                "header injection."
            )
    return token
