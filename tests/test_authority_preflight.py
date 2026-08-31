"""Unit tests for `command_center.orchestrator.authority_preflight`.

Regression anchor: VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-UNPRIVILEGED-
EXECUTOR — a task whose body demanded `sudo` / a specific PostgreSQL role
reached the model executor cascade anyway, which honestly tried and failed
(no worker in this fleet has that privilege), burning three model calls per
cascade and looping forever through `cascade_exhausted: task_status_failed`.
These tests lock the preflight decision that must catch this BEFORE the
planner ever calls `backlog_dispatch`.
"""

from __future__ import annotations

from command_center.orchestrator import authority_preflight as ap


# --------------------------------------------------------------------------
# The fleet grants nothing today — the whole point of the incident.
# --------------------------------------------------------------------------


def test_fleet_grants_no_authority_today():
    assert ap.FLEET_GRANTED_AUTHORITY == frozenset()


# --------------------------------------------------------------------------
# Declared authority — the `Requires-Authority:` field.
# --------------------------------------------------------------------------


def test_declared_root_is_recognized():
    tags = ap.declared_authority("Requires-Authority: root\nDo the thing.")
    assert tags == frozenset({ap.AUTHORITY_ROOT})


def test_declared_sudo_alias_normalizes_to_root():
    assert ap.declared_authority("Requires-Authority: sudo") == frozenset({ap.AUTHORITY_ROOT})


def test_declared_bare_postgres_normalizes_to_postgres_role_postgres():
    tags = ap.declared_authority("Requires-Authority: postgres")
    assert tags == frozenset({ap.POSTGRES_ROLE_PREFIX + "postgres"})


def test_declared_named_postgres_role_is_preserved():
    tags = ap.declared_authority("Requires-Authority: postgres-role:readonly")
    assert tags == frozenset({ap.POSTGRES_ROLE_PREFIX + "readonly"})


def test_declared_multiple_comma_separated_tokens():
    tags = ap.declared_authority("Requires-Authority: root, postgres-role:readonly")
    assert tags == frozenset({ap.AUTHORITY_ROOT, ap.POSTGRES_ROLE_PREFIX + "readonly"})


def test_declared_unrecognized_token_becomes_external_credential_not_dropped():
    tags = ap.declared_authority("Requires-Authority: stripe-live-api-key")
    assert tags == frozenset({ap.EXTERNAL_CREDENTIAL_PREFIX + "stripe-live-api-key"})


def test_declared_field_is_case_insensitive_and_bullet_tolerant():
    tags = ap.declared_authority("- REQUIRES-AUTHORITY: Root")
    assert tags == frozenset({ap.AUTHORITY_ROOT})


def test_no_declared_field_yields_empty():
    assert ap.declared_authority("Just a normal task description.") == frozenset()


# --------------------------------------------------------------------------
# Detected authority — the narrow command-shaped safety net.
# --------------------------------------------------------------------------


def test_detects_the_exact_incident_commands():
    """The literal commands the parked worker log showed
    (VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE): `sudo /usr/bin/true` and
    `sudo -u postgres /usr/bin/psql -c 'select 1'`."""
    body = (
        "Run `sudo /usr/bin/true` and "
        "`sudo -u postgres /usr/bin/psql -c 'select 1'` to confirm access."
    )
    tags = ap.detected_authority(body)
    assert tags == frozenset({ap.AUTHORITY_ROOT, ap.POSTGRES_ROLE_PREFIX + "postgres"})


def test_detects_sudo_with_known_admin_verb():
    assert ap.detected_authority("sudo apt-get install -y ripgrep") == frozenset({ap.AUTHORITY_ROOT})
    assert ap.detected_authority("sudo systemctl restart aicc-worker") == frozenset({ap.AUTHORITY_ROOT})


def test_detects_root_prose_directive():
    tags = ap.detected_authority("The task requires root access to rotate the credential.")
    assert tags == frozenset({ap.AUTHORITY_ROOT})


def test_detects_postgres_role_switch_command():
    assert ap.detected_authority("psql -U postgres -c 'select 1'") == frozenset(
        {ap.POSTGRES_ROLE_PREFIX + "postgres"}
    )
    assert ap.detected_authority("su - postgres") == frozenset({ap.POSTGRES_ROLE_PREFIX + "postgres"})


def test_does_not_misfire_on_prose_that_merely_discusses_sudo_and_postgres():
    """The false-positive guard this module's own docstring promises: prose
    that talks ABOUT sudo/PostgreSQL access must never be mistaken for a task
    that actually demands it — `sudo` followed by an ordinary English word is
    not a command."""
    text = (
        "This module documents why sudo and postgres access matter for "
        "security, and describes sudoers file handling and PostgreSQL "
        "configuration in general."
    )
    assert ap.detected_authority(text) == frozenset()


def test_benign_implementation_prompt_detects_nothing():
    assert ap.detected_authority("Fix the bug in the parser and add tests.") == frozenset()


# --------------------------------------------------------------------------
# The decision.
# --------------------------------------------------------------------------


def test_decide_ok_for_a_benign_task():
    decision = ap.decide("VOYN-W0-FIX", "Fix the bug in the parser and add tests.")
    assert decision.ok
    assert decision.required == frozenset()
    assert decision.missing == frozenset()
    assert decision.reason is None


def test_decide_blocks_the_incident_shape():
    decision = ap.decide(
        "VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE",
        "Verify: run `sudo /usr/bin/true` and `sudo -u postgres psql -c 'select 1'`.",
    )
    assert not decision.ok
    assert decision.missing == frozenset({ap.AUTHORITY_ROOT, ap.POSTGRES_ROLE_PREFIX + "postgres"})
    assert decision.reason == ap.park_reason(decision)


def test_decide_combines_title_and_body():
    decision = ap.decide("Requires-Authority: root", "do the thing")
    assert not decision.ok
    assert decision.missing == frozenset({ap.AUTHORITY_ROOT})


def test_decide_none_title_and_body_is_ok():
    decision = ap.decide(None, None)
    assert decision.ok
    assert decision.required == frozenset()


def test_missing_is_required_minus_granted():
    decision = ap.decide("t", "Requires-Authority: root")
    assert decision.missing == decision.required - decision.granted


# --------------------------------------------------------------------------
# The park reason — the machine-readable classification.
# --------------------------------------------------------------------------


def test_park_reason_prefix_is_distinct_from_cascade_exhausted():
    """`backlog_resume_deferred` (0014) and the planner's own auto-resume
    query match ONLY `cascade_exhausted:%` — a requires-authority park must
    never be auto-resumed, since no retry fixes a privilege the fleet does
    not have."""
    decision = ap.decide("t", "Requires-Authority: root")
    reason = ap.park_reason(decision)
    assert reason.startswith(ap.PARK_REASON_PREFIX)
    assert not reason.startswith("cascade_exhausted:")


def test_park_reason_is_deterministic_and_sorted():
    decision = ap.decide("t", "Requires-Authority: root, postgres-role:readonly")
    assert ap.park_reason(decision) == (
        "requires_privileged_authority: postgres_role:readonly,root"
    )


def test_format_authority_empty_set():
    assert ap.format_authority(frozenset()) == "(none)"
