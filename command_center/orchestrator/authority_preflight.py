"""Pre-dispatch authority preflight (VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-
UNPRIVILEGED-EXECUTOR).

Background — the defect this module closes
--------------------------------------------
Found live 2026-08-30, measured on the recovered queue: 90 minutes after
dispatch resumed, 8 of 32 dispatches ended `cascade_exhausted:
task_status_failed`. The worker log for one (`VOYN-W0-AICC-CONTROL-PLANE-
RESILIENCE`) showed the agent honestly running `sudo /usr/bin/true` and
`sudo -u postgres /usr/bin/psql -c 'select 1'`, both refused (`a password is
required`). Every host in this fleet is deliberately unprivileged (no sudo,
no PostgreSQL role) — the sandbox is not the bug. The bug is that a task
demanding a privilege no executor holds ever reached the model at all: the
cascade (`orchestrator.routing`) only chooses AMONG claude/codex/copilot,
every one of which runs the same unprivileged worker, so retrying across the
cascade cannot ever succeed for this class of failure. 0012's own
classification then makes it worse: `cascade_exhausted: task_status_failed`
is TECHNICAL, so `backlog_return_to_pool` sends the task straight back to
OPEN — the planner re-dispatches it, it fails the same way, forever, three
model calls at a time.

The fix has to live before dispatch, not after. This module is the pure
decision: given a task's own title/body, what authority does it require, and
does this fleet grant it? The planner (`orchestrator.planner`) calls
`decide()` for every OPEN candidate BEFORE `backlog_dispatch` — a task this
fleet cannot satisfy is parked straight to `DEFER_TO_USER` via
`backlog_park_requires_authority` (0017) and never claims a WIP slot, never
enters the cascade, and never spends a single model call finding out the
hard way.

The model
---------
An **authority** is one system-level privilege a task's *work*, not its
*prose*, demands: root/sudo, a specific PostgreSQL role, or a named external
credential. Two ways a task states one:

- **Declared** — a `Requires-Authority: <token>[, <token>...]` line anywhere
  in the title or body (the same "field embedded in prose" convention this
  backlog already uses for `Acceptance:`/`Depends on:`). This is the primary,
  precise surface: authors say exactly what they need.
- **Detected** — a narrow, deterministic regex safety net (no LLM call, same
  discipline as `command_center.capabilities.prompt_requires_write`) for the
  concrete shapes already proven to fail: an actual `sudo` invocation, or a
  `-u postgres` / `-U postgres` / `su postgres` role switch. Deliberately
  narrow: it must never fire on prose that merely *discusses* sudo or
  PostgreSQL (this very docstring, for instance) — only on what reads as a
  command.

`FLEET_GRANTED_AUTHORITY` is what this fleet actually grants today: nothing.
Every worker is the same unprivileged, sandboxed task-clone executor: that is
enforced by design and re-provable independently of this module (12h of
worker logs, zero sandbox denials). If a privileged worker lane is ever
added, its granted authority extends this set and satisfied tasks flow
through normally — this module never needs to change for that, only its
constant does.

Pure functions only — no I/O, no subprocess, no database. Trivially unit
testable and safe to import from any layer (leaf module; imports nothing
from `command_center`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "AUTHORITY_ROOT",
    "AuthorityDecision",
    "FLEET_GRANTED_AUTHORITY",
    "PARK_REASON_PREFIX",
    "decide",
    "declared_authority",
    "detected_authority",
    "format_authority",
    "park_reason",
    "required_authority",
]

# --------------------------------------------------------------------------
# Authority tags.
# --------------------------------------------------------------------------

#: Root / sudo on the executing host.
AUTHORITY_ROOT = "root"

#: `postgres_role:<role>` — membership in a specific PostgreSQL role, e.g.
#: `postgres_role:postgres`. The role name travels in the tag so two
#: different-role requirements are distinct requirements.
POSTGRES_ROLE_PREFIX = "postgres_role:"

#: `external_credential:<name>` — a named credential this fleet does not
#: provision (an API key, a third-party account, ...). Declared-only: there
#: is no reliable prose signal for "this needs a credential" that would not
#: also match ordinary implementation work ("add the credential to config").
EXTERNAL_CREDENTIAL_PREFIX = "external_credential:"

#: What THIS fleet's executors grant, today. Empty on purpose: every
#: executor in `orchestrator.routing.ROUTING_MATRIX` (claude/codex/copilot)
#: runs as the same unprivileged, sandboxed task-clone worker — none holds
#: root or a PostgreSQL role. See the module docstring for the live evidence.
FLEET_GRANTED_AUTHORITY: frozenset[str] = frozenset()

# --------------------------------------------------------------------------
# Declared authority — `Requires-Authority: <token>[, <token>...]`.
# --------------------------------------------------------------------------

_DECLARED_LINE = re.compile(
    r"(?im)^[ \t]*(?:[-*][ \t]+)?requires[ \t]*-[ \t]*authority[ \t]*:[ \t]*(.+?)[ \t]*$"
)

# Bare aliases a task author may write for the common cases, normalized to
# the canonical tag. Anything else declared becomes an external-credential
# tag verbatim — an unrecognized declared token still fails closed (it is
# never silently dropped from `required`), it just cannot be named "root" or
# "postgres_role" by accident.
_DECLARED_ALIASES: dict[str, str] = {
    "root": AUTHORITY_ROOT,
    "sudo": AUTHORITY_ROOT,
    "postgres": POSTGRES_ROLE_PREFIX + "postgres",
}


def declared_authority(text: str) -> frozenset[str]:
    """Authority tags from every `Requires-Authority:` line in `text`."""
    tags: set[str] = set()
    for match in _DECLARED_LINE.finditer(text):
        for token in match.group(1).split(","):
            token = token.strip().lower()
            if not token:
                continue
            if token in _DECLARED_ALIASES:
                tags.add(_DECLARED_ALIASES[token])
            elif token.startswith(("postgres-role:", "postgres_role:", "db-role:", "db_role:")):
                role = token.split(":", 1)[1].strip()
                if role:
                    tags.add(POSTGRES_ROLE_PREFIX + role)
            else:
                tags.add(EXTERNAL_CREDENTIAL_PREFIX + token)
    return frozenset(tags)


# --------------------------------------------------------------------------
# Detected authority — the narrow command-shaped regex safety net.
# --------------------------------------------------------------------------

# An actual `sudo` invocation, not merely the word "sudo" in prose (e.g. this
# module's own docstring). Matched only when what follows unambiguously reads
# as a command: a flag (`-u`, `-i`, ...), an absolute path, or a known
# system-administration verb -- an ordinary English word ("sudo and X") never
# matches any of the three.
_SUDO_KNOWN_VERBS = (
    r"(?:apt(?:-get)?|systemctl|service|useradd|usermod|chown|chmod|mkdir|"
    r"psql|su|reboot|shutdown|mount|umount|iptables|ufw|tee|kill|pkill|"
    r"docker|yum|dnf|pacman)"
)
_SUDO_COMMAND = re.compile(
    rf"\bsudo\s+(?:-\S+(?:\s+\S+)?|/\S+|{_SUDO_KNOWN_VERBS}\b)"
)
# An explicit request for elevated access, stated in prose rather than a
# command — the declared-field convention is preferred, but a plain sentence
# saying so must still be caught (fail closed on ambiguity, never lower the
# requirement).
_ROOT_PROSE = re.compile(r"\brequires?\s+(?:root|elevated|superuser)\s+(?:access|privileges?|permissions?)\b", re.I)
# A PostgreSQL role switch shaped like an actual command: `-u postgres`,
# `-U postgres`, or `su [-] postgres`.
_POSTGRES_ROLE_COMMAND = re.compile(r"(?:-[uU]\s+postgres\b)|(?:\bsu\s+(?:-\s+)?postgres\b)")


def detected_authority(text: str) -> frozenset[str]:
    """Authority tags from concrete privileged-command shapes in `text`
    (no declared field required). Deliberately narrow — see module docstring."""
    tags: set[str] = set()
    if _SUDO_COMMAND.search(text) or _ROOT_PROSE.search(text):
        tags.add(AUTHORITY_ROOT)
    if _POSTGRES_ROLE_COMMAND.search(text):
        tags.add(POSTGRES_ROLE_PREFIX + "postgres")
    return frozenset(tags)


def required_authority(title: str | None, body: str | None) -> frozenset[str]:
    """Every authority tag `title`/`body` declares or plainly demands."""
    text = f"{title or ''}\n{body or ''}"
    return declared_authority(text) | detected_authority(text)


def format_authority(tags) -> str:
    """Deterministic, comma-joined rendering of an authority tag set."""
    return ",".join(sorted(tags)) or "(none)"


# --------------------------------------------------------------------------
# The decision.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorityDecision:
    """The full, self-describing result of resolving one task's authority
    requirement against what this fleet grants."""

    required: frozenset[str]
    granted: frozenset[str]
    missing: frozenset[str]
    declared: frozenset[str]
    detected: frozenset[str]
    ok: bool
    reason: str | None  # machine-readable park reason, or None when ok


def decide(title: str | None, body: str | None) -> AuthorityDecision:
    """Resolve the authority `title`/`body` requires against
    `FLEET_GRANTED_AUTHORITY`. Total: never raises, always answers."""
    text = f"{title or ''}\n{body or ''}"
    declared = declared_authority(text)
    detected = detected_authority(text)
    required = declared | detected
    granted = FLEET_GRANTED_AUTHORITY
    missing = required - granted
    ok = not missing
    reason = None if ok else park_reason_for(missing)
    return AuthorityDecision(
        required=required,
        granted=granted,
        missing=missing,
        declared=declared,
        detected=detected,
        ok=ok,
        reason=reason,
    )


# Machine-readable park reason prefix. Distinct from `cascade_exhausted:%`
# on purpose: `backlog_resume_deferred` (0014) and the planner's own
# technical-park auto-resume query (`planner.py`, the `resumable` CTE) both
# match ONLY `cascade_exhausted:%` — a requires-authority park must never be
# auto-resumed, because no retry fixes a privilege the fleet does not have;
# only an owner acting (granting the privilege, adding a privileged worker
# lane, or rewriting the task) can.
PARK_REASON_PREFIX = "requires_privileged_authority: "


def park_reason_for(missing) -> str:
    return PARK_REASON_PREFIX + format_authority(missing)


def park_reason(decision: AuthorityDecision) -> str:
    """The `backlog_park_requires_authority` reason for a blocked decision."""
    return decision.reason or park_reason_for(decision.missing)
