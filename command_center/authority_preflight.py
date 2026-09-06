"""Executor-authority preflight — does a dispatched task actually demand host
privileges (root/sudo) or a specific PostgreSQL OS/DB role, and does the
executor about to run it hold that authority?

Background — the incident this module closes
----------------------------------------------
Found live on the restored dispatch queue (2026-08-30): in 90 minutes, 8 of
32 dispatches were `cascade_exhausted: task_status_failed`. Reading one
worker journal end to end showed the agent honestly trying
``sudo /usr/bin/true`` and ``sudo -u postgres psql -c 'select 1'``, both
failing with "a password is required", then exiting unsuccessfully — the
cascade then repeated the identical, unwinnable attempt twice more. The task
needed root or the `postgres` OS role; the executor that claimed it has
neither by design. Nothing checked that mismatch before the model was
invoked, so three attempts (three cascade links, three model calls) were
spent discovering something a string match could have decided for free.

The model
---------
An **authority** is a privilege no worker holds unless an operator has
explicitly granted it (unlike a Claude Code *tool*, which `capabilities.py`
already gates — this module is about the *operating system's* permission
boundary underneath the tool, not the tool itself). Two are recognized,
matching the two the incident evidence names:

- ``AUTHORITY_ROOT`` — the task must run something as root (``sudo``, or
  prose that says as much).
- ``AUTHORITY_POSTGRES_ROLE`` — the task must act as the ``postgres`` OS/DB
  role (``sudo -u postgres ...``, ``su postgres``, or prose that says as
  much).

`decide()` is the whole point: given the authorities a task requires
(explicit payload declaration, unioned with what the prompt's own text
demands) and the authorities this worker has been granted, it reports
whether the grant covers the need. The worker calls this *before* the model
is ever invoked (`worker.handlers._run_agent`) and refuses to dispatch when
it does not — a deterministic, machine-reasoned `BLOCKED`, not a spent
cascade attempt that resolves to `task_status_failed`.

Detection is prose analysis, not command execution: two prior attempts at
this module got it wrong in opposite directions, and both regressions are
pinned by this module's test suite —

1. A narrow verb allowlist for "what counts as a sudo command" missed
   `sudo cp`, `sudo rm`, `sudo bash -c ...`, `sudo python ...` — genuinely
   privileged commands that sailed through preflight into the same failure
   loop this module exists to prevent. Fixed by matching `sudo` plus *any*
   following token, not a fixed verb list.
2. Matching `-u postgres` anywhere, context-free, false-positived on
   `docker run -u postgres ...` (a container UID flag, not a host PostgreSQL
   role switch). Fixed by requiring the *escalation command itself*
   (`sudo`/`su`) to be the one naming `postgres`, not merely the substring
   appearing somewhere in the line.
3. Root-prose matching ignored negation: "This does not require root
   access" matched `require...root...access` and parked the task forever.
   Fixed by scanning the clause immediately preceding a match for negation
   phrasing (`does not`, `don't`, `without`, ...) before accepting it.
4. Command-shaped text matched anywhere in the body, including quoted
   examples, prohibitions, and test specifications — "add a test ensuring
   `sudo rm` is rejected", "document why users must not run `sudo apt`",
   "sanitize the string `su postgres`" — none of which require the
   *executor* to hold any authority at all. Fixed by the same
   preceding-clause scan recognizing a bounded list of meta/mention verbs
   (test, ensure, document, sanitize, reject, prohibit, ...) and suppressing
   the match when one frames it.

Both fixes share one mechanism (`_unguarded_matches`): a match only counts
when the ~80 characters (or the current clause, whichever is shorter)
immediately before it contain neither a negation phrase nor a meta/mention
verb. This is a bounded, deterministic heuristic — not an LLM call, not a
claim of perfect natural-language understanding — scoped narrowly enough
that ordinary task prose is not misclassified, per this project's established
convention (see `capabilities.py`'s prompt-intent detection and
`runtime.outcome`'s blocker-language detection for the same shape of
trade-off elsewhere in this codebase).

Pure functions only — no I/O, no subprocess, no database. Safe to import
from any layer.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = [
    "AUTHORITY_ROOT",
    "AUTHORITY_POSTGRES_ROLE",
    "ALL_AUTHORITIES",
    "AUTHORITY_LABELS",
    "AuthorityDecision",
    "required_authorities",
    "worker_granted_authorities",
    "decide",
    "failure_reason_code",
    "FAILURE_REASON_PREFIX",
]

# --------------------------------------------------------------------------
# Authorities.
# --------------------------------------------------------------------------

AUTHORITY_ROOT = "root"
AUTHORITY_POSTGRES_ROLE = "postgres_role"

ALL_AUTHORITIES: frozenset[str] = frozenset({AUTHORITY_ROOT, AUTHORITY_POSTGRES_ROLE})

AUTHORITY_LABELS: dict[str, str] = {
    AUTHORITY_ROOT: "root/sudo",
    AUTHORITY_POSTGRES_ROLE: "the postgres OS/DB role",
}


def _sort_authorities(authorities) -> list[str]:
    order = {AUTHORITY_ROOT: 0, AUTHORITY_POSTGRES_ROLE: 1}
    return sorted(authorities, key=lambda name: (order.get(name, 2), name))


def format_authorities(authorities) -> str:
    return "/".join(_sort_authorities(authorities)) or "(none)"


# --------------------------------------------------------------------------
# Guard vocabulary — shared by every detector below.
#
# A match only counts when the clause immediately preceding it contains
# neither of these. Deliberately bounded lists (not general negation/NLU):
# narrow enough that ordinary imperative task prose ("run `sudo systemctl
# restart nginx`") is unaffected, wide enough to cover the two concrete
# false-positive shapes review found (negated requirement statements, and
# command mentions framed by test/doc/prohibition verbs).
# --------------------------------------------------------------------------

_NEGATION_GUARD: list[re.Pattern[str]] = [
    re.compile(r"\bdoes\s+not\b", re.I),
    re.compile(r"\bdo\s+not\b", re.I),
    re.compile(r"\bdon'?t\b", re.I),
    re.compile(r"\bdoesn'?t\b", re.I),
    re.compile(r"\bdidn'?t\b", re.I),
    re.compile(r"\bmust\s+not\b", re.I),
    re.compile(r"\bshould\s+not\b", re.I),
    re.compile(r"\bshouldn'?t\b", re.I),
    re.compile(r"\bmustn'?t\b", re.I),
    re.compile(r"\bwithout\b", re.I),
    re.compile(r"\bnever\s+require", re.I),
    re.compile(r"\bnot\s+require", re.I),
    re.compile(r"\bnot\s+need", re.I),
]

# Meta/mention verbs: a command-shaped or authority-shaped phrase framed by
# one of these, in the clause right before it, is being discussed/tested/
# documented/prohibited rather than actually demanded of the executor.
_MENTION_GUARD = re.compile(
    r"\b(?:"
    r"test\w*|ensur\w*|verif\w*|"
    r"document\w*|describ\w*|mention\w*|"
    r"example\w*|illustrat\w*|demonstrat\w*|"
    r"sanitiz\w*|string|quote\w*|quoting|comment\w*|docstring\w*|"
    r"reject\w*|prohibit\w*|disallow\w*|forbid\w*|warn\w*|avoid\w*"
    r")\b",
    re.I,
)

# How far back (chars) a guard is looked for, bounded further by the start
# of the current clause (previous sentence/clause terminator) if that is
# closer. Forward-only framing ("run `sudo ...`, which we document later")
# is intentionally NOT suppressed -- all four regression examples this
# module pins frame the command/phrase from the left.
_GUARD_WINDOW = 80


def _preceding_clause(text: str, start: int) -> str:
    boundary = max(
        text.rfind(".", 0, start),
        text.rfind("\n", 0, start),
        text.rfind(";", 0, start),
        text.rfind("!", 0, start),
        text.rfind("?", 0, start),
        text.rfind(":", 0, start),
    )
    left = max(boundary + 1, start - _GUARD_WINDOW)
    return text[left:start]


def _unguarded_matches(pattern: re.Pattern[str], text: str) -> bool:
    for match in pattern.finditer(text):
        clause = _preceding_clause(text, match.start())
        if _MENTION_GUARD.search(clause):
            continue
        if any(guard.search(clause) for guard in _NEGATION_GUARD):
            continue
        return True
    return False


# --------------------------------------------------------------------------
# Detectors.
# --------------------------------------------------------------------------

# `sudo` plus at least one following token -- deliberately verb-agnostic
# (fix for false negative #1: a fixed verb allowlist missed `sudo cp`,
# `sudo rm`, `sudo bash -c ...`, `sudo python ...`). Matches the command
# form regardless of which program follows.
_SUDO_COMMAND = re.compile(r"\bsudo\b(?:\s+\S+)+", re.I)

_ROOT_PROSE = re.compile(
    r"\b(?:requires?|needs?|need)\s+(?:root|superuser|administrator|elevated)\s+"
    r"(?:access|privileges?|permissions?|rights?)\b"
    r"|\brun(?:s|ning)?\s+as\s+root\b"
    r"|\brequires?\s+sudo\b",
    re.I,
)

# The escalation tool itself must be the one naming `postgres` (fix for
# false negative #2: `docker run -u postgres ...` context-free matched a
# bare `-u postgres` substring, but `docker` is not a host privilege
# escalation to the postgres role).
_SUDO_POSTGRES_ROLE = re.compile(r"\bsudo\b(?:\s+-{1,2}\S+)*\s+-u\s+postgres\b", re.I)
_SU_POSTGRES_ROLE = re.compile(r"\bsu\b\s+(?:-\s*)?postgres\b", re.I)

_POSTGRES_ROLE_PROSE = re.compile(
    r"\b(?:requires?|needs?|need)\s+(?:the\s+)?postgres\s+(?:role|user|superuser)\b"
    r"|\bas\s+the\s+postgres\s+(?:role|user)\b",
    re.I,
)


def required_authorities(prompt: str | None) -> frozenset[str]:
    """The authorities `prompt`'s own text plainly demands of the executor,
    or an empty set for `None`/empty text or text with no such demand.

    A match is only accepted when the clause introducing it is not itself a
    negation ("does not require root access") or a meta/mention framing
    ("add a test ensuring `sudo rm` is rejected") -- see the module
    docstring for the incidents this scoping fixes.
    """
    if not prompt:
        return frozenset()
    required: set[str] = set()
    if _unguarded_matches(_SUDO_COMMAND, prompt) or _unguarded_matches(_ROOT_PROSE, prompt):
        required.add(AUTHORITY_ROOT)
    if (
        _unguarded_matches(_SUDO_POSTGRES_ROLE, prompt)
        or _unguarded_matches(_SU_POSTGRES_ROLE, prompt)
        or _unguarded_matches(_POSTGRES_ROLE_PROSE, prompt)
    ):
        required.add(AUTHORITY_POSTGRES_ROLE)
    return frozenset(required)


# --------------------------------------------------------------------------
# Granted authorities — what this worker host has been explicitly given.
#
# Empty by default: the fleet's workers hold no host privilege unless an
# operator opts one in, matching the incident's own root cause ("sandbox is
# fine, apparmor profile is fine, the executor simply has no path to root or
# to the postgres role"). Reading from the environment (not a database) keeps
# this a per-host, per-process fact -- exactly the granularity the grant
# actually has (systemd unit / operator-provisioned host), and keeps this
# module's promise of no I/O for its pure functions (the read happens once,
# at the call site, not inside `decide`).
# --------------------------------------------------------------------------

_GRANT_ENV_VAR = "AICC_WORKER_AUTHORITIES"


def worker_granted_authorities(env: dict | None = None) -> frozenset[str]:
    """Authorities this worker process has been granted, from
    ``AICC_WORKER_AUTHORITIES`` (comma-separated, e.g. ``"root,postgres_role"``).
    Unrecognized entries are ignored rather than raising -- a typo in an
    operator-set env var must fail closed (grant nothing extra), not crash
    the worker."""
    source = env if env is not None else os.environ
    raw = source.get(_GRANT_ENV_VAR, "")
    granted = {
        entry.strip()
        for entry in raw.split(",")
        if entry.strip() in ALL_AUTHORITIES
    }
    return frozenset(granted)


# --------------------------------------------------------------------------
# The decision.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorityDecision:
    required: list[str]
    granted: list[str]
    missing: list[str]
    ok: bool
    reason: str | None


def build_missing_authority_reason(missing: frozenset[str]) -> str:
    return (
        "Executor authority mismatch: task requires "
        f"{format_authorities(missing)}; this executor holds neither. "
        "Route to an operator/executor explicitly granted it "
        f"(set {_GRANT_ENV_VAR})."
    )


def decide(
    prompt: str | None,
    granted: frozenset[str],
    *,
    declared: frozenset[str] = frozenset(),
) -> AuthorityDecision:
    """Resolve the authorities a task needs (the payload's own explicit
    `declared` set, unioned with whatever the prompt text plainly demands)
    against `granted` -- the authorities this executor actually holds.

    `declared` is the primary channel (a payload that already knows it needs
    root/the postgres role should say so directly); prompt detection is the
    fallback signal for payloads that predate or omit that declaration, and
    can only ever add authorities, never remove one the payload declared.
    """
    required = declared | required_authorities(prompt)
    missing = required - granted
    ok = not missing
    reason = None if ok else build_missing_authority_reason(missing)
    return AuthorityDecision(
        required=_sort_authorities(required),
        granted=_sort_authorities(granted),
        missing=_sort_authorities(missing),
        ok=ok,
        reason=reason,
    )


# Machine-readable `failure_reason`/outcome-reason prefix for a preflight
# authority mismatch. Distinct from `capabilities.py`'s `capability_mismatch:`
# (a Claude Code *tool* mismatch) and from the cascade's own
# `cascade_exhausted:`/`executor_unavailable:` reasons, so downstream
# classification never folds a missing-authority block into the generic
# `task_status_failed` bucket the incident report measured.
FAILURE_REASON_PREFIX = "missing_authority:"


def failure_reason_code(decision: AuthorityDecision) -> str:
    return FAILURE_REASON_PREFIX + ",".join(decision.missing)
