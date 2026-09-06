"""Unit tests for `command_center.authority_preflight`.

Regression anchor: the incident this module closes -- a task whose prompt
demanded `sudo /usr/bin/true` and `sudo -u postgres psql ...` was dispatched
to an executor with neither authority, discovered only after three spent
model attempts (`cascade_exhausted: task_status_failed`).

This suite also pins the two prior, rejected attempts at this exact module
(PR #530, PR #571) so their specific regressions cannot recur silently:

- #530: a narrow sudo-verb allowlist missed `sudo cp`/`sudo rm`/
  `sudo bash -c`/`sudo python`; and context-free `-u postgres` matching
  false-positived on `docker run -u postgres ...`.
- #571: root-prose detection ignored negation ("This does not require root
  access"); and command-shaped text matched even inside quoted examples,
  prohibitions, and test specifications.
"""

from __future__ import annotations

import pytest

from command_center import authority_preflight as ap


# --------------------------------------------------------------------------
# required_authorities -- true positives.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "Run `sudo cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak` before editing it.",
        "Use sudo rm -rf /var/cache/app to clear the stale cache.",
        "You'll need to sudo bash -c 'systemctl daemon-reload' on the host.",
        "Execute sudo python3 /opt/tools/migrate.py --apply.",
        "sudo apt-get install -y postgresql-client on the target host.",
        "Restart the unit with sudo systemctl restart nginx once deployed.",
        "This task requires root access to modify /etc/hosts.",
        "The fix needs elevated privileges on the production host.",
        "This must run as root because it rewrites /etc/shadow.",
    ],
)
def test_root_authority_detected_for_genuine_commands_and_prose(prompt):
    assert ap.AUTHORITY_ROOT in ap.required_authorities(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "Connect with sudo -u postgres psql -c 'select 1'.",
        "sudo -u postgres pg_dump mydb > backup.sql",
        "su postgres -c 'psql -c \\'select 1\\''",
        "su - postgres and run createdb myapp.",
        "This task requires the postgres role to run the migration.",
        "Connect as the postgres user to verify the schema.",
    ],
)
def test_postgres_role_authority_detected_for_genuine_commands_and_prose(prompt):
    assert ap.AUTHORITY_POSTGRES_ROLE in ap.required_authorities(prompt)


def test_sudo_postgres_role_command_requires_both_authorities():
    # The exact incident command: escalating via sudo AND switching to the
    # postgres role in one invocation.
    required = ap.required_authorities("Run sudo -u postgres psql -c 'select 1' to confirm connectivity.")
    assert required == {ap.AUTHORITY_ROOT, ap.AUTHORITY_POSTGRES_ROLE}


# --------------------------------------------------------------------------
# required_authorities -- PR #530 regressions (false negatives / false
# positives that let the broken behavior through review).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "sudo cp file /dest",
        "sudo rm -rf /tmp/build",
        "sudo bash -c 'echo hi'",
        "sudo python script.py",
    ],
)
def test_broadened_sudo_verb_coverage(prompt):
    """A verb allowlist previously missed these -- sudo plus ANY following
    token must count, not a fixed set of verbs."""
    assert ap.AUTHORITY_ROOT in ap.required_authorities(prompt)


def test_docker_run_u_postgres_is_not_a_postgres_role_requirement():
    """`-u postgres` is a container UID flag here, not a host PostgreSQL role
    switch -- the escalation command must itself be sudo/su naming postgres,
    not merely the substring `-u postgres` appearing anywhere."""
    prompt = "Run the migration inside `docker run -u postgres --rm myimage:latest migrate`."
    required = ap.required_authorities(prompt)
    assert ap.AUTHORITY_POSTGRES_ROLE not in required
    assert ap.AUTHORITY_ROOT not in required


# --------------------------------------------------------------------------
# required_authorities -- PR #571 regressions.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "This does not require root access, just edit the config file.",
        "This task doesn't need root privileges.",
        "No root access is needed here -- it's a plain file edit.",
        "The change should not require sudo at all.",
    ],
)
def test_negated_root_prose_is_not_a_requirement(prompt):
    assert ap.AUTHORITY_ROOT not in ap.required_authorities(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "Add a test ensuring `sudo rm` is rejected by the input sanitizer.",
        "Document why users must not run `sudo apt` on this host.",
        "Sanitize the string `su postgres` before logging it.",
        "Write a regression test verifying `sudo -u postgres psql` is refused.",
    ],
)
def test_quoted_prohibited_or_test_specified_commands_are_not_requirements(prompt):
    required = ap.required_authorities(prompt)
    assert ap.AUTHORITY_ROOT not in required
    assert ap.AUTHORITY_POSTGRES_ROLE not in required


# --------------------------------------------------------------------------
# required_authorities -- ordinary prose stays unaffected.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "Review the module and summarize the findings.",
        "Refactor the parser for clarity; no privileged operations involved.",
        "Add unit tests for the retry logic.",
        "",
        None,
    ],
)
def test_ordinary_prompts_require_no_authority(prompt):
    assert ap.required_authorities(prompt) == frozenset()


# --------------------------------------------------------------------------
# decide() -- the preflight decision.
# --------------------------------------------------------------------------


def test_decide_ok_when_nothing_required():
    decision = ap.decide("summarize the findings", frozenset())
    assert decision.ok
    assert decision.missing == []
    assert decision.reason is None


def test_decide_blocks_when_required_but_not_granted():
    decision = ap.decide("Run sudo systemctl restart nginx.", frozenset())
    assert not decision.ok
    assert decision.missing == [ap.AUTHORITY_ROOT]
    assert decision.reason is not None
    assert "root" in decision.reason


def test_decide_ok_when_granted_covers_requirement():
    decision = ap.decide(
        "Run sudo -u postgres psql -c 'select 1'.",
        frozenset({ap.AUTHORITY_ROOT, ap.AUTHORITY_POSTGRES_ROLE}),
    )
    assert decision.ok
    assert decision.missing == []


def test_decide_declared_authority_is_required_even_without_prompt_match():
    """The payload's own explicit declaration is honored even when the
    prompt text alone would not have tripped the detector -- the primary
    channel, prompt detection is only the fallback."""
    decision = ap.decide(
        "Apply the configuration change.",
        frozenset(),
        declared=frozenset({ap.AUTHORITY_POSTGRES_ROLE}),
    )
    assert not decision.ok
    assert decision.missing == [ap.AUTHORITY_POSTGRES_ROLE]


def test_decide_declared_and_prompt_detected_union():
    decision = ap.decide(
        "Also run sudo systemctl restart nginx.",
        frozenset(),
        declared=frozenset({ap.AUTHORITY_POSTGRES_ROLE}),
    )
    assert decision.missing == [ap.AUTHORITY_ROOT, ap.AUTHORITY_POSTGRES_ROLE]


def test_failure_reason_code_is_distinct_from_capability_mismatch():
    decision = ap.decide("Run sudo -u postgres psql.", frozenset())
    code = ap.failure_reason_code(decision)
    assert code.startswith(ap.FAILURE_REASON_PREFIX)
    assert "capability_mismatch" not in code
    assert "root" in code and "postgres_role" in code


# --------------------------------------------------------------------------
# worker_granted_authorities()
# --------------------------------------------------------------------------


def test_worker_granted_authorities_defaults_to_empty():
    assert ap.worker_granted_authorities(env={}) == frozenset()


def test_worker_granted_authorities_parses_comma_list():
    granted = ap.worker_granted_authorities(env={"AICC_WORKER_AUTHORITIES": "root, postgres_role"})
    assert granted == {ap.AUTHORITY_ROOT, ap.AUTHORITY_POSTGRES_ROLE}


def test_worker_granted_authorities_ignores_unknown_entries():
    granted = ap.worker_granted_authorities(env={"AICC_WORKER_AUTHORITIES": "root,nonsense"})
    assert granted == {ap.AUTHORITY_ROOT}
