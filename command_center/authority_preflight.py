"""Required-authority preflight — "does this executor actually hold the
authority this task needs?", decided *before* a single model token is spent.

Background — the incident this module closes
--------------------------------------------
2026-08-30, on the restored queue: 32 dispatches produced 13 returns to the
pool, the largest group being 8 x ``cascade_exhausted: task_status_failed``.
One of them (``VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE``) reads, in the
worker's journal, as an agent honestly trying ``sudo /usr/bin/true`` and
``sudo -u postgres psql -c 'select 1'``, being told ``a password is
required``, and giving up. The cascade then reproduced that refusal twice
more. Nothing was broken: the sandbox was healthy (bwrap 0.9.0, zero sandbox
refusals in 12 hours) and the worker principal is unprivileged *by design*
(ADR-0010). The defect was routing — a task that needs authority the worker
does not have, and by design must never have, was dispatched to it anyway,
and the mismatch was discovered only by burning three model attempts.

The doctrine: authority is DECLARED, never inferred from prose
--------------------------------------------------------------
Three earlier attempts at this module tried to *read the task's prompt* and
guess whether it needed root/postgres. Each was rejected in review, and each
rejection found the same class of defect from a different side:

1. A verb allow-list (``sudo apt``/``sudo systemctl``/...) let every verb
   nobody listed — ``sudo cp``, ``sudo rm``, ``sudo bash -c`` — straight
   through into the failure loop the module exists to prevent.
2. Widening to "any ``sudo``-shaped token" then parked ordinary work:
   "add a test ensuring ``sudo rm`` is rejected" needs no privilege at all,
   and a context-free ``-u postgres`` match parked ``docker run -u postgres``
   which needs no host role either. Negated prose ("this does **not** require
   root") parked too.
3. Adding negation/mention guard words made it worse in the dangerous
   direction: the guard was a bag-of-words scan over a fixed window with no
   syntactic link to the match, so "Update the connection **string**, then
   run ``sudo systemctl restart postgres``" suppressed a genuinely required
   command and silently returned an authority set that was missing ``root``.

The lesson is not "write a better regex". It is that a prompt is prose, the
question is semantic, and a scanner that answers it will be wrong in both
directions — over-parking honest work and, worse, *silently* under-reporting
real requirements, which reproduces the original incident with no signal at
all. This module therefore does not scan prompts. It reads a **declaration**:

- ``required``  — a machine field on the payload (``required_authorities``),
  set by the backlog record. Absent means "nothing beyond the ordinary
  unprivileged workspace", which is the historical behaviour byte-for-byte.
- ``granted``   — what this worker actually holds, deny-by-default: the
  deployment's own declaration (``AICC_WORKER_AUTHORITIES``), *narrowed* by a
  real probe wherever a cheap deterministic probe exists. Configuration can
  only ever take authority away from the verifiable set, never add it.

`decide()` compares the two sets. That is set arithmetic over declared data:
it has no false positives and no false negatives, because there is nothing to
infer. The same reasoning is already recorded in
``agent_runner.RunResult.is_executor_api_error``, which refuses to classify
infrastructure failures from free result text for exactly this reason.

What this design can and cannot promise
---------------------------------------
It cannot know what an **undeclared** task needs — nobody can, without either
a declaration or an observation. What it guarantees is that the requirement
has to be learned exactly once and never costs a cascade again:

- declared up front (owner sets it on the backlog task) -> the mismatch is a
  deterministic ``BLOCKED`` before the model is launched: zero model calls,
  zero attempts consumed, a machine reason naming the missing authority;
- discovered by a run -> the executor reports it through the
  ``REQUIRES_AUTHORITY:`` trailer (the same explicit machine-trailer contract
  as ``HEAD_SHA:`` and ``SPLIT_TASKS_JSON:``; see `declared_by_run`), ingest
  records it on the task and parks it for the owner instead of re-running the
  identical refusal on every remaining cascade link.

Routing, and the boundary of the refusal
----------------------------------------
"Route it to an executor that holds the authority" is a DEPLOYMENT act: a host
that genuinely holds it declares so through `GRANT_ENV_VAR`, and the same
`decide()` then lets the task run there instead of blocking it. The worker
side needs no new concept for that.

The refusal is non-retryable, and that is a judgement about today's fleet, not
a universal truth -- so it is stated rather than buried. Every executor in the
pool is the same unprivileged principal (ADR-0010), so a second delivery lands
on an identically unprivileged host and reproduces the identical refusal;
retrying would spend the attempt budget to re-derive a known answer, which is
the incident. If the fleet ever runs a genuinely heterogeneous pool -- some
hosts privileged, some not -- this refusal should become a *routing* signal
(return to the pool so a capable host can claim it) rather than a terminal
one, exactly as `handlers._executor_preflight` already treats an unavailable
executor. Until such a host exists, returning it to the pool would be the
phantom-link hazard `orchestrator.routing` warns about: a retry that cannot
succeed, dressed as one that might.

Trailer trust is deliberate and bounded: an executor that emits the trailer
spuriously parks its own task for the owner, which is visible, reversible and
strictly safer than the dead-lettering it replaces. It cannot grant authority,
only ask for it. This is the trust level `SPLIT_TASKS_JSON` already carries.

Pure except for one clearly-marked section: everything above `# -- probes --`
is set arithmetic with no I/O, so it is trivially unit-testable and safe to
import from any layer. The probes take their subprocess runner and environment
as parameters, so they are testable without a privileged host.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

__all__ = [
    "AUTHORITY_EXTERNAL_CREDENTIAL",
    "AUTHORITY_ORDER",
    "AUTHORITY_POSTGRES_ROLE",
    "AUTHORITY_ROOT",
    "AUTHORITY_VOCABULARY",
    "GRANT_ENV_VAR",
    "PROBE_TIMEOUT_SECONDS",
    "REASON_SATISFIED",
    "REASON_UNAVAILABLE",
    "AuthorityDecision",
    "AuthorityDeclarationError",
    "decide",
    "declared_by_run",
    "normalize_authorities",
    "preflight",
    "probe_postgres_role",
    "probe_root",
    "worker_granted_authorities",
]

# --------------------------------------------------------------------------
# The vocabulary. Closed on purpose: an authority nobody can name is an
# authority nobody can grant, probe or audit, so an unrecognized name is a
# payload defect (see `normalize_authorities`) rather than a silent pass.
# These are exactly the three the acceptance criteria name -- elevated host
# privilege, database access under a specific role, and credentials for a
# service outside this host.
# --------------------------------------------------------------------------

#: Passwordless elevated privilege on the worker host (`sudo`, root-owned
#: paths, host service management).
AUTHORITY_ROOT = "root"
#: A PostgreSQL session as a specific *host* role (the incident's
#: `sudo -u postgres psql`). Not "the app can reach its own database": the
#: worker always has its own DSN, and that is not this.
AUTHORITY_POSTGRES_ROLE = "postgres_role"
#: A credential for a service outside this host that the worker principal is
#: not issued (deploy keys, registry tokens, provider admin credentials).
AUTHORITY_EXTERNAL_CREDENTIAL = "external_credential"

#: Stable order used everywhere an authority set is rendered — reason
#: strings, payload fields, persisted metadata — so the same set always
#: serializes to the same text and a test can pin it.
AUTHORITY_ORDER: tuple[str, ...] = (
    AUTHORITY_ROOT,
    AUTHORITY_POSTGRES_ROLE,
    AUTHORITY_EXTERNAL_CREDENTIAL,
)
AUTHORITY_VOCABULARY: frozenset[str] = frozenset(AUTHORITY_ORDER)
_ORDER_INDEX = {name: index for index, name in enumerate(AUTHORITY_ORDER)}

#: Machine reason codes. These are the strings the queue, the backlog park
#: classifier and the reports agree on; they are never assembled ad hoc at a
#: call site.
REASON_SATISFIED = "authority_satisfied"
REASON_UNAVAILABLE = "authority_unavailable"

#: The environment variable through which a deployment declares what the
#: worker principal holds. Absent/empty means "nothing", which is both the
#: safe default and the truth on every isolated worker today (ADR-0010).
GRANT_ENV_VAR = "AICC_WORKER_AUTHORITIES"


class AuthorityDeclarationError(ValueError):
    """A declaration that is not a list of recognized authority names.

    Fail closed and loudly: silently ignoring an unparseable declaration
    would hand the task to whatever executor happened to claim it while the
    author believed they had constrained it — the exact failure this module
    exists to prevent, with a false audit trail on top."""


def normalize_authorities(value: object, *, field: str = "required_authorities") -> tuple[str, ...]:
    """Canonicalize a declaration into a deduplicated, `AUTHORITY_ORDER`-sorted
    tuple.

    ``None``/absent/``[]`` is "no special authority" — the historical default.
    Names are matched case-insensitively after trimming; anything that is not
    a list of recognized names raises `AuthorityDeclarationError`."""
    if value is None:
        return ()
    # A list or tuple, and nothing else. Deliberately narrow: a `str` is
    # iterable and a `dict` iterates its keys, so an `isinstance(Iterable)`
    # test would happily "parse" a task body one character at a time, or read
    # a mapping's keys as a declaration. Both are exactly the accidental
    # prose-reading this module exists to make impossible.
    if not isinstance(value, (list, tuple)):
        raise AuthorityDeclarationError(
            f"{field} must be a list of authority names, got {type(value).__name__}"
        )
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            raise AuthorityDeclarationError(
                f"{field} entries must be strings, got {type(entry).__name__}"
            )
        name = entry.strip().lower()
        if name not in AUTHORITY_VOCABULARY:
            raise AuthorityDeclarationError(
                f"{field} names unknown authority {entry!r}; "
                f"known: {sorted(AUTHORITY_VOCABULARY)}"
            )
        seen.add(name)
    return tuple(sorted(seen, key=_ORDER_INDEX.__getitem__))


def _ordered(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(names), key=lambda n: _ORDER_INDEX.get(n, len(_ORDER_INDEX))))


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    """The preflight verdict. ``ok`` is the whole question; the rest is the
    audit trail the park reason and the work result carry."""

    ok: bool
    required: tuple[str, ...]
    granted: tuple[str, ...]
    missing: tuple[str, ...]
    reason_code: str

    @property
    def reason(self) -> str:
        """The machine reason string. Stable by construction: a sorted set
        rendered through `AUTHORITY_ORDER`, never interpolated prose."""
        if self.ok:
            return f"{REASON_SATISFIED}: required={list(self.required)}"
        return (
            f"{REASON_UNAVAILABLE}: task requires {list(self.missing)} "
            f"which this executor does not hold "
            f"(required={list(self.required)}, granted={list(self.granted)}); "
            f"route to an executor holding it, or grant it to the owner"
        )


def decide(required: Iterable[str], granted: Iterable[str]) -> AuthorityDecision:
    """Set arithmetic, nothing else: a task may run when every authority it
    declares is one this executor holds.

    Both arguments are already-normalized name sets. There is deliberately no
    prompt, no task body and no heuristic in this signature — the module
    docstring records why."""
    required_set = _ordered(required)
    granted_set = _ordered(granted)
    missing = _ordered(set(required_set) - set(granted_set))
    return AuthorityDecision(
        ok=not missing,
        required=required_set,
        granted=granted_set,
        missing=missing,
        reason_code=REASON_SATISFIED if not missing else REASON_UNAVAILABLE,
    )


# --------------------------------------------------------------------------
# The executor's own report, through an explicit machine trailer.
#
# This is NOT prompt scanning: it reads the executor's final message for a
# line the prompt contract asks for, in the same shape and with the same
# anchoring discipline as `handlers._HEAD_SHA_TRAILER` -- start of line,
# end of line, a closed vocabulary between them. A task *body* is never
# passed here; only a completed run's own result text.
# --------------------------------------------------------------------------

_AUTHORITY_TRAILER = re.compile(
    r"^REQUIRES_AUTHORITY:[ \t]*([A-Za-z_]+(?:[ \t]*,[ \t]*[A-Za-z_]+)*)[ \t]*$",
    re.MULTILINE,
)


def declared_by_run(result_text: str) -> tuple[str, ...]:
    """The authorities a finished run reported it needed and did not have.

    Empty when there is no trailer, when the trailer is malformed, or when it
    names nothing recognized: an executor's typo must not crash ingest, and
    the task simply falls through to the ordinary outcome path it would have
    taken before this contract existed. Multiple trailers union — an executor
    that discovers a second requirement mid-run reports both.

    Anchored per line and bounded to the closed vocabulary, so prose that
    merely *discusses* the trailer cannot satisfy it unless it is literally
    emitted as its own line; and the worst case if it is, is a park visible
    to the owner (see the module docstring on trailer trust)."""
    found: set[str] = set()
    for match in _AUTHORITY_TRAILER.finditer(result_text or ""):
        for raw in match.group(1).split(","):
            name = raw.strip().lower()
            if name in AUTHORITY_VOCABULARY:
                found.add(name)
    return _ordered(found)


# --------------------------------------------------------------------------
# -- probes -- the only impure section.
#
# Deny by default. A deployment DECLARES what its worker principal holds; for
# every authority that has a cheap deterministic probe, the declaration is
# then VERIFIED and dropped if the probe disagrees. Configuration can only
# narrow the verifiable set, never widen it past reality -- a stale
# `AICC_WORKER_AUTHORITIES=root` on a host that lost its sudoers entry must
# not re-create the incident by asserting authority that is not there.
#
# The probes are the incident's own commands, made non-interactive: `sudo -n`
# and `psql -w` never wait for a password, so a worker without the authority
# fails in milliseconds instead of hanging until the run times out.
# --------------------------------------------------------------------------

Runner = Callable[..., subprocess.CompletedProcess]

#: A probe must never become the new hang. Both probes are a single local
#: syscall-bound command; anything slower than this is a "no".
PROBE_TIMEOUT_SECONDS = 5


def _run_quietly(runner: Runner, argv: list[str], env: Mapping[str, str] | None = None) -> bool:
    """True only when the command ran and exited 0. Every other outcome — a
    non-zero exit, a missing binary, a timeout, an OS refusal — is "this
    authority is not held", because the question is whether the authority is
    *usable right now*, not why it is not."""
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def probe_root(runner: Runner = subprocess.run) -> bool:
    """``sudo -n true``: the incident's own probe, with ``-n`` so it refuses
    instead of prompting. Exit 0 means this principal can elevate without a
    password; anything else means it cannot."""
    if shutil.which("sudo") is None:
        return False
    return _run_quietly(runner, ["sudo", "-n", "true"])


def probe_postgres_role(
    role: str = "postgres",
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> bool:
    """``psql -w -U <role> -c 'select 1'``: ``-w`` never prompts, and a short
    ``PGCONNECT_TIMEOUT`` keeps an unreachable server from stalling dispatch.
    Exit 0 means a session as that role is genuinely available here."""
    if shutil.which("psql") is None:
        return False
    probe_env = dict(env if env is not None else os.environ)
    probe_env["PGCONNECT_TIMEOUT"] = str(PROBE_TIMEOUT_SECONDS)
    return _run_quietly(
        runner, ["psql", "-w", "-U", role, "-tAc", "select 1"], env=probe_env
    )


#: Authority -> verifier. An authority absent from this table has no cheap
#: deterministic probe (there is no generic "do I hold some external
#: credential" question), so the deployment's declaration stands for it. The
#: asymmetry is deliberate and stated rather than hidden: verify what can be
#: verified, and never *invent* verification that would be a guess.
_VERIFIERS: dict[str, Callable[..., bool]] = {
    AUTHORITY_ROOT: probe_root,
    AUTHORITY_POSTGRES_ROLE: probe_postgres_role,
}


def worker_granted_authorities(
    *,
    needed: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> tuple[str, ...]:
    """What this executor actually holds, out of the authorities `needed`.

    ``needed`` scopes the work: a task declaring nothing (the overwhelming
    majority) probes nothing and costs nothing, so dispatch latency is
    unchanged for every run that existed before this module. Passing an empty
    ``needed`` therefore returns ``()`` — "nothing was asked, nothing was
    checked" — which is exactly what `decide` wants, since an empty
    requirement is satisfied by an empty grant.

    An unparseable `AICC_WORKER_AUTHORITIES` entry is dropped rather than
    raised on: a deployment typo must not take the worker off the fleet, and
    dropping fails in the safe direction (the task parks for the owner)."""
    wanted = set(_ordered(needed))
    if not wanted:
        return ()
    environ = env if env is not None else os.environ
    declared = {
        entry.strip().lower()
        for entry in (environ.get(GRANT_ENV_VAR, "") or "").split(",")
        if entry.strip().lower() in AUTHORITY_VOCABULARY
    }
    held: set[str] = set()
    for name in declared & wanted:
        verifier = _VERIFIERS.get(name)
        if verifier is None or verifier(runner=runner):
            held.add(name)
    return _ordered(held)


def preflight(
    required: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> AuthorityDecision:
    """The one call a dispatcher makes: normalize, probe only what the task
    asked for, and decide. No model process exists at this point and none is
    started if the decision is not ``ok``."""
    wanted = _ordered(required)
    return decide(wanted, worker_granted_authorities(needed=wanted, env=env, runner=runner))
