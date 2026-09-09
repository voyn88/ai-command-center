"""One GitHub identity, one quota, one cache for every control tick.

Why this exists
---------------
The review, merge and PR-window ticks all shell out to ``gh`` with whatever
credential happens to be ambient on control-01 -- which is one human's OAuth
token in ``~/.config/gh/hosts.yml`` (``dimastov-lab``). ``gh pr list`` and
``gh pr view`` are GraphQL calls, GraphQL quota is per USER, and that same
user's token is also what the operator's laptop tools spend. On 2026-09-09
between 21:15 and 22:10 UTC every tick died on::

    GraphQL: API rate limit already exceeded for user ID 297853521

The PR-window tick could not label, and the review/merge ticks skipped every
task -- for as long as the human's hour-long window stayed exhausted. Nothing
in the control plane had its own quota, so a human running ``gh`` on a laptop
could stop the fleet (VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS).

Three changes, all here or driven from here:

1. **Identity.** The fleet already has a GitHub App -- ``voyn-aicc-fleet``,
   registered for the isolated worker lanes (VOYN-W0-AICC-ISOLATED-WORKER-
   NEEDS-READ-ONLY-GIT-ACCESS) -- whose installation token carries its OWN
   REST and GraphQL quota, entirely separate from any human's.
   ``voyn-aicc-github-token.timer`` already mints it into
   ``/var/lib/aicc/github`` every 30 minutes, 0640 root:aicc-worker, and the
   control ticks run as ``aicc-worker``: they can read it today. ``run()``
   points ``gh`` at that store's ``GH_CONFIG_DIR`` (clearing ``GH_TOKEN`` /
   ``GITHUB_TOKEN``, which would otherwise win over it) whenever the store is
   present and unexpired, and falls back to the ambient credential otherwise
   rather than failing the tick -- a host without the store (a developer
   laptop, CI, a control host whose App key was never placed) keeps working
   exactly as before, and says so in the telemetry.

2. **REST over GraphQL in the hot loops.** ``gh pr list``/``gh pr view`` are
   GraphQL; ``gh api repos/...`` is REST, a different and much larger budget
   (5000 requests/hour) that is measured per request rather than in node
   points. The PR-window loop -- which touches EVERY open pull request on
   every tick -- is served from REST endpoints through the helpers here;
   see ``review_merge`` for the call sites.

3. **Cache per (repo, PR, head).** A pull request whose head has not moved
   cannot have changed its checks-and-reviews verdict in a way the window
   labeller must react to within seconds, so its details are cached on disk
   keyed by the exact head sha and re-read for ``ttl`` seconds. A push
   changes the sha and invalidates the entry on the spot -- the cache can
   serve stale data for at most ``ttl``, never for a head that no longer
   exists.
   Deliberately NOT used by the merge tick: merging is an irreversible act on
   the strength of an ACCEPT marker and a green rollup, and those two must be
   read fresh, never from a cache written up to ``ttl`` seconds ago.

Every call is counted (`GhQuota`), and each tick ends with one
``gh api rate_limit`` -- an endpoint that does not itself consume quota -- so
the tick report says which identity it used and how much of that identity's
budget is left. That is the telemetry the incident had no way to show.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "GhIdentity",
    "GhQuota",
    "PrDetailCache",
    "current_quota",
    "detail_cache",
    "resolve_identity",
    "run",
    "tick",
]

#: Where `voyn-aicc-github-token.timer` puts the fleet App's `gh` config
#: (`hosts.yml` + `config.yml`), and the token store root beside it that also
#: holds `expires_at`. Overridable for tests and for a host that keeps the
#: store elsewhere.
FLEET_CONFIG_DIR_ENV = "AICC_GH_FLEET_CONFIG_DIR"
DEFAULT_FLEET_CONFIG_DIR = "/var/lib/aicc/github/gh"

#: `auto` (default) uses the fleet App when its store is usable; `ambient`
#: forces the ambient credential -- an operator escape hatch for the case
#: where the App's installation is the thing that is broken.
IDENTITY_ENV = "AICC_GH_IDENTITY"

CACHE_DIR_ENV = "AICC_GH_CACHE_DIR"
CACHE_TTL_ENV = "AICC_GH_CACHE_TTL_SECONDS"
#: Half of the PR-window tick's 15-minute period times two: a head that has
#: not moved is re-read every other tick at most. Long enough that the steady
#: state of a large open-PR list costs almost no API calls, short enough that
#: a label follows a check flip within one further tick.
DEFAULT_CACHE_TTL_SECONDS = 1800.0

_TIMEOUT_SECONDS = 120

#: Identity names as they appear in the tick report.
FLEET = "fleet-app"
AMBIENT = "ambient"
INHERITED = "inherited"

#: A refused call that says something about the CREDENTIAL rather than about
#: one resource: after one of these the whole tick drops to the ambient
#: credential instead of paying a doubled call count on every subsequent
#: lookup.
_CREDENTIAL_FAILURES = (
    "http 401",
    "bad credentials",
    "token expired",
)
#: A refused call that says something about THIS resource under an otherwise
#: healthy credential: the App may simply not hold the permission (it is
#: granted contents/pull-requests, not actions or checks on every host) or not
#: be installed on this repository. Retried once on the ambient credential so
#: a best-effort path like a workflow rerun keeps working.
_RESOURCE_FAILURES = (
    "resource not accessible by integration",
    "http 403",
    "http 404",
    "must have admin rights",
    # GraphQL's way of saying the token cannot see this repository or pull
    # request -- about the RESOURCE, not the credential, so it must not
    # demote the whole tick the way a 401 does.
    "could not resolve to",
)
_RATE_LIMIT_MARKERS = (
    "api rate limit exceeded",
    "rate limit already exceeded",
    "secondary rate limit",
    "was submitted too quickly",
)


@dataclass(frozen=True, slots=True)
class GhIdentity:
    """Which credential `gh` runs under, and the environment that selects it.

    `env` is an overlay on the caller's own environment: a value of None
    REMOVES the variable, which is how the fleet identity is made to stick
    (`GH_TOKEN`/`GITHUB_TOKEN` take precedence over `GH_CONFIG_DIR` inside
    `gh`, so leaving an inherited one in place would silently keep spending
    the human's quota while the report claimed otherwise)."""

    name: str
    reason: str
    env: Mapping[str, str | None] = field(default_factory=dict)


@dataclass(slots=True)
class GhQuota:
    """Per-tick GitHub telemetry: identity, call mix, cache and budget.

    Attached to the tick reports and printed by `command_center.db.cli`, so
    an exhausted quota is a visible line in the tick's own output rather
    than something an operator has to reconstruct from `gh` stderr."""

    identity: str = AMBIENT
    identity_reason: str = "not_resolved"
    #: `gh api ...` -- REST, 5000 requests/hour, the hot loops' transport.
    rest_calls: int = 0
    #: `gh pr ...`/`gh run ...` -- GraphQL-backed porcelain, node-point budget.
    graphql_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    #: Calls that were re-run on the ambient credential after the fleet App
    #: refused them (missing permission, not installed, expired token).
    ambient_fallbacks: int = 0
    #: Calls GitHub refused for rate limiting -- the incident's own signal.
    rate_limited: int = 0
    core_remaining: int | None = None
    core_limit: int | None = None
    graphql_remaining: int | None = None
    graphql_limit: int | None = None
    #: Epoch second at which the core budget resets, as GitHub reports it.
    core_reset: int | None = None
    #: Set once a credential-level refusal has demoted the whole tick.
    degraded: bool = False

    @property
    def calls(self) -> int:
        return self.rest_calls + self.graphql_calls

    def line(self) -> str:
        """One line for the tick report. Always printed, including when the
        rate-limit probe itself could not run (`?`), because 'we could not
        read the budget' is itself worth seeing."""

        def budget(remaining: int | None, limit: int | None) -> str:
            if remaining is None:
                return "?"
            return f"{remaining}/{limit}" if limit is not None else str(remaining)

        parts = [
            f"identity={self.identity}",
            f"calls={self.calls} (rest={self.rest_calls} graphql={self.graphql_calls})",
            f"cache={self.cache_hits}/{self.cache_hits + self.cache_misses}",
            f"core={budget(self.core_remaining, self.core_limit)}",
            f"graphql={budget(self.graphql_remaining, self.graphql_limit)}",
        ]
        if self.ambient_fallbacks:
            parts.append(f"ambient_fallbacks={self.ambient_fallbacks}")
        if self.rate_limited:
            parts.append(f"rate_limited={self.rate_limited}")
        if self.identity != FLEET:
            parts.append(f"reason={self.identity_reason}")
        return "QUOTA     " + " ".join(parts)


_ACTIVE_QUOTA: ContextVar[GhQuota | None] = ContextVar("aicc_gh_quota", default=None)


def current_quota() -> GhQuota | None:
    """The quota counter of the enclosing `tick()`, or None outside one."""
    return _ACTIVE_QUOTA.get()


def _parse_expiry(text: str) -> float | None:
    stamp = text.strip()
    if stamp.endswith("Z"):
        stamp = stamp[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def resolve_identity(
    env: Mapping[str, str] | None = None, *, now: float | None = None
) -> GhIdentity:
    """Pick the credential this host's control ticks should use.

    Order, most specific first:

    * ``AICC_GH_IDENTITY=ambient`` -- the operator has said so explicitly.
    * an inherited ``GH_CONFIG_DIR`` -- a unit (or an operator) already
      pointed ``gh`` somewhere deliberate; this module does not second-guess
      it, and reports it as `inherited` rather than claiming the fleet App.
    * the fleet App store -- present, readable, and not expired.
    * ambient, with the reason it fell back, so the tick report can say why
      it is still spending a human's quota.
    """
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    requested = (env.get(IDENTITY_ENV) or "auto").strip().lower()
    if requested == AMBIENT:
        return GhIdentity(AMBIENT, "requested_by_env")
    if env.get("GH_CONFIG_DIR"):
        return GhIdentity(INHERITED, "gh_config_dir_inherited")
    config_dir = Path(env.get(FLEET_CONFIG_DIR_ENV) or DEFAULT_FLEET_CONFIG_DIR)
    hosts = config_dir / "hosts.yml"
    if not os.access(hosts, os.R_OK):
        return GhIdentity(AMBIENT, "fleet_store_unreadable")
    expires_at = config_dir.parent / "expires_at"
    try:
        stamp = expires_at.read_text(encoding="utf-8")
    except OSError:
        stamp = ""
    if stamp.strip():
        expiry = _parse_expiry(stamp)
        # An UNPARSEABLE stamp is not evidence of expiry: the token itself is
        # what GitHub validates, so a format this code does not recognise
        # must not disable the fleet identity. An expired one is different --
        # every call would 401, and one ambient fallback per call is worse
        # than starting ambient.
        if expiry is not None and now >= expiry:
            return GhIdentity(AMBIENT, "fleet_token_expired")
    return GhIdentity(
        FLEET,
        "fleet_app_token_store",
        {
            "GH_CONFIG_DIR": str(config_dir),
            # These win over GH_CONFIG_DIR inside `gh`; an inherited one would
            # keep spending the human's quota under a report that says fleet.
            "GH_TOKEN": None,
            "GITHUB_TOKEN": None,
            "GH_HOST": None,
        },
    )


_IDENTITY_TTL_SECONDS = 60.0
_identity_cache: tuple[float, GhIdentity] | None = None


def _identity(*, refresh: bool = False) -> GhIdentity:
    """Resolved at most once a minute: the store is three stat calls, and a
    tick can make hundreds of `gh` calls. `tick()` forces a refresh so a
    token minted between ticks is picked up immediately."""
    global _identity_cache
    now = time.time()
    if not refresh and _identity_cache is not None:
        resolved_at, identity = _identity_cache
        if now - resolved_at < _IDENTITY_TTL_SECONDS:
            return identity
    identity = resolve_identity(now=now)
    _identity_cache = (now, identity)
    return identity


def _apply(env: Mapping[str, str | None]) -> dict[str, str]:
    merged = dict(os.environ)
    for key, value in env.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _matches(stderr: str, markers: tuple[str, ...]) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in markers)


def _spawn(
    argv: list[str], cwd: str, env: Mapping[str, str | None], timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=_apply(env),
    )


def run(
    argv: list[str], cwd: str, *, timeout: int = _TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """`gh <argv>` under the control plane's own identity, counted.

    The single choke point every control-tick `gh` call goes through, so the
    identity, the fallback and the accounting are decided in one place rather
    than at ~40 call sites. Signature and return value are `subprocess.run`'s,
    unchanged, because the callers already branch on `returncode`/`stdout`."""
    quota = current_quota()
    identity = _identity()
    if quota is not None and quota.degraded and identity.name == FLEET:
        # A credential-level refusal already happened this tick; do not pay a
        # doubled call for every remaining lookup.
        identity = GhIdentity(AMBIENT, "fleet_credential_refused")
    if quota is not None:
        # `gh api <path>` is REST; every `gh pr`/`gh run` porcelain resolves
        # through GraphQL, and `gh api graphql` says so itself. The split is
        # what makes the report actionable: the two budgets are separate and
        # only one of them was ever the problem.
        if argv[:1] == ["api"] and argv[1:2] != ["graphql"]:
            quota.rest_calls += 1
        else:
            quota.graphql_calls += 1
    proc = _spawn(argv, cwd, identity.env, timeout)
    stderr = proc.stderr or ""
    # A rate-limited call is never retried on the ambient credential: the
    # whole point of the fleet identity is to stop the control plane
    # spending a human's budget, and doing it precisely when a budget is
    # already exhausted would just double the damage. It is counted instead,
    # and the tick report says so.
    limited = _matches(stderr, _RATE_LIMIT_MARKERS)
    if proc.returncode != 0 and identity.name == FLEET and not limited:
        credential = _matches(stderr, _CREDENTIAL_FAILURES)
        if credential or _matches(stderr, _RESOURCE_FAILURES):
            if quota is not None:
                quota.ambient_fallbacks += 1
                quota.degraded = quota.degraded or credential
            proc = _spawn(argv, cwd, {}, timeout)
            stderr = proc.stderr or ""
            limited = _matches(stderr, _RATE_LIMIT_MARKERS)
    if proc.returncode != 0 and limited and quota is not None:
        quota.rate_limited += 1
    return proc


def record_rate_limit(cwd: str, quota: GhQuota) -> None:
    """Fill in the budget half of the telemetry with one `gh api rate_limit`.

    That endpoint is explicitly exempt from rate limiting, so the measurement
    never costs what it measures. Best-effort: an unreadable budget leaves the
    fields None (`?` in the report) rather than failing a tick that has
    otherwise already done its work."""
    identity = _identity()
    if quota.degraded and identity.name == FLEET:
        identity = GhIdentity(AMBIENT, "fleet_credential_refused")
    try:
        proc = _spawn(["api", "rate_limit"], cwd, identity.env, 30)
    except (OSError, subprocess.SubprocessError):
        return
    if proc.returncode != 0:
        return
    try:
        resources = (json.loads(proc.stdout or "{}") or {}).get("resources") or {}
    except ValueError:
        return
    core = resources.get("core") or {}
    graphql = resources.get("graphql") or {}
    for source, remaining, limit in (
        (core, "core_remaining", "core_limit"),
        (graphql, "graphql_remaining", "graphql_limit"),
    ):
        if isinstance(source.get("remaining"), int):
            setattr(quota, remaining, source["remaining"])
        if isinstance(source.get("limit"), int):
            setattr(quota, limit, source["limit"])
    if isinstance(core.get("reset"), int):
        quota.core_reset = core["reset"]


@contextmanager
def tick(cwd: str | None = None) -> Iterator[GhQuota]:
    """Scope one control tick: fresh counters, a re-resolved identity, and a
    budget reading on the way out.

    Nested use is a no-op re-entry (the inner scope yields the outer counter),
    so a tick that calls another tick's helper still reports one total."""
    existing = current_quota()
    if existing is not None:
        yield existing
        return
    identity = _identity(refresh=True)
    quota = GhQuota(identity=identity.name, identity_reason=identity.reason)
    token = _ACTIVE_QUOTA.set(quota)
    try:
        yield quota
    finally:
        _ACTIVE_QUOTA.reset(token)
        if cwd is not None and quota.calls:
            record_rate_limit(cwd, quota)


# -- the (repo, PR, head) detail cache ----------------------------------------


def _cache_root(env: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if env is None else env
    explicit = env.get(CACHE_DIR_ENV)
    if explicit:
        candidates = [Path(explicit)]
    else:
        candidates = []
        # systemd `CacheDirectory=` on the tick units: owned by the unit's own
        # User, which is the only writable well-known location a control tick
        # running as `aicc-worker` actually has.
        state = env.get("CACHE_DIRECTORY")
        if state:
            candidates.append(Path(state.split(":")[0]) / "gh-details")
        home_cache = env.get("XDG_CACHE_HOME") or env.get("HOME")
        if home_cache:
            base = Path(home_cache)
            if not env.get("XDG_CACHE_HOME"):
                base = base / ".cache"
            candidates.append(base / "aicc" / "gh-details")
        candidates.append(
            Path(tempfile.gettempdir()) / f"aicc-gh-details-{os.getuid()}"
        )
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(candidate, os.W_OK):
            return candidate
    return None


def _cache_ttl(env: Mapping[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    try:
        ttl = float(env.get(CACHE_TTL_ENV) or DEFAULT_CACHE_TTL_SECONDS)
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS
    return max(ttl, 0.0)


class PrDetailCache:
    """Disk cache of per-pull-request details keyed by (repo, number, head).

    The head sha is part of the KEY, not merely of the payload: a push makes
    the old entry unreachable rather than stale, so the only staleness this
    can ever serve is a change that did not move the head (a review posted, a
    check finishing) for at most `ttl` seconds. Entirely best-effort -- an
    unwritable directory, a truncated file or a foreign JSON shape all
    degrade to a miss, never to an exception in a tick."""

    def __init__(self, directory: Path | None, ttl: float, *, now: Any = time.time):
        self.directory = directory
        self.ttl = ttl
        self._now = now

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PrDetailCache:
        ttl = _cache_ttl(env)
        return cls(_cache_root(env) if ttl else None, ttl)

    @property
    def enabled(self) -> bool:
        return self.directory is not None and self.ttl > 0

    def _path(self, repo: str, number: int, head: str) -> Path:
        digest = hashlib.sha256(f"{repo}\x1f{number}\x1f{head}".encode()).hexdigest()
        assert self.directory is not None
        return self.directory / f"{digest}.json"

    def get(self, repo: str, number: int, head: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            raw = self._path(repo, number, head).read_text(encoding="utf-8")
            entry = json.loads(raw)
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict):
            return None
        # The key is re-checked from the payload, so a digest collision or a
        # hand-edited file can never hand one PR another PR's checks.
        if (
            entry.get("repo") != repo
            or entry.get("number") != number
            or entry.get("head") != head
        ):
            return None
        fetched_at = entry.get("fetched_at")
        if not isinstance(fetched_at, (int, float)):
            return None
        if self._now() - float(fetched_at) > self.ttl:
            return None
        payload = entry.get("payload")
        return payload if isinstance(payload, dict) else None

    def put(
        self, repo: str, number: int, head: str, payload: dict[str, Any]
    ) -> None:
        if not self.enabled:
            return
        path = self._path(repo, number, head)
        entry = {
            "repo": repo,
            "number": number,
            "head": head,
            "fetched_at": self._now(),
            "payload": payload,
        }
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(entry), encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            try:
                tmp.unlink()
            except OSError:
                pass

    def prune(self, *, keep_factor: float = 4.0) -> int:
        """Drop entries far past their TTL so a long-lived cache directory
        does not accumulate one file per head sha the fleet ever pushed.

        Judged by file mtime rather than by reading each entry: pruning must
        stay one `stat` per file, since it runs once per tick over every
        head the window has ever seen. Called once per tick, never per
        lookup."""
        if not self.enabled:
            return 0
        assert self.directory is not None
        horizon = self._now() - max(self.ttl * keep_factor, self.ttl)
        removed = 0
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if entry.suffix != ".json":
                continue
            try:
                if entry.stat().st_mtime < horizon:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


def detail_cache(env: Mapping[str, str] | None = None) -> PrDetailCache:
    """The per-tick cache handle. Pruned on construction (once per tick)."""
    cache = PrDetailCache.from_env(env)
    cache.prune()
    return cache
