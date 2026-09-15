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


# --------------------------------------------------------------------------
# Instruction position vs quotation (VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-
# UNPRIVILEGED-EXECUTOR, second finding).
#
# The first cut of the detector fired on the command SHAPE anywhere in the
# text. Every task in this backlog is an incident report that QUOTES the
# command that failed, so that detector parked repair tasks -- terminally,
# since a requires-authority park is deliberately outside the
# `cascade_exhausted:%` vocabulary the 0014 reconcile auto-resumes.
# --------------------------------------------------------------------------


#: This task's own body, verbatim (the live 2026-08-30 incident report). It
#: requires no privilege whatsoever -- its work is Python and SQL -- and the
#: shape-only detector returned {root, postgres_role:postgres} for it.
_THIS_TASKS_OWN_BODY = (
    "AICC Platform / Delivery Cost | **Найдено живьём 2026-08-30 (Claude), "
    "измерено на восстановленной очереди.** За 90 минут после восстановления "
    "диспетчеризации: 32 `dispatch`, из них 13 `return_to_pool` (~40%). "
    "Крупнейшая группа — 8 × `cascade_exhausted: task_status_failed`. Разбор "
    "одной (`VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE`) по журналу воркера: агент "
    "в своём task-клоне честно пробовал `sudo /usr/bin/true` и "
    "`sudo -u postgres /usr/bin/psql -c 'select 1'`, получил "
    "`a password is required` и завершился неуспехом."
)


def test_quoted_incident_evidence_does_not_park_this_very_task():
    """The regression anchor: the task that fixes the bug must survive its
    own fix. Quoted evidence is a suspicion, never a requirement."""
    decision = ap.decide(
        "VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-UNPRIVILEGED-EXECUTOR",
        _THIS_TASKS_OWN_BODY,
    )
    assert decision.ok, "an incident report quoting `sudo` must not be parked"
    assert decision.required == frozenset()
    assert decision.reason is None
    # Not silently dropped, either: it is visible as a suspicion.
    assert decision.suspected == frozenset(
        {ap.AUTHORITY_ROOT, ap.POSTGRES_ROLE_PREFIX + "postgres"}
    )


def test_english_narrative_of_an_attempt_is_not_an_order():
    """`tried to run` is a postmortem narrating, not a task ordering: the
    verb sits mid-clause after `to`, never at a clause start."""
    body = "The agent tried to run `sudo /usr/bin/true` and was refused."
    assert ap.detected_authority(body) == frozenset()
    assert ap.suspected_authority(body) == frozenset({ap.AUTHORITY_ROOT})


def test_blockquoted_worker_log_is_a_quotation_not_an_order():
    body = "The worker log shows:\n> sudo -u postgres psql -c 'select 1'\n"
    assert ap.detected_authority(body) == frozenset()
    # `>` is a quotation marker, so BOTH tags the line carries stay suspicions.
    assert ap.suspected_authority(body) == frozenset(
        {ap.AUTHORITY_ROOT, ap.POSTGRES_ROLE_PREFIX + "postgres"}
    )


def test_imperative_at_a_clause_start_is_an_order():
    for body in (
        "Verify resilience: run `sudo /usr/bin/true` to confirm access.",
        "Run `sudo systemctl restart aicc-worker`.",
        "Fix the unit. Then execute `sudo systemctl daemon-reload`.",
    ):
        assert ap.detected_authority(body) == frozenset({ap.AUTHORITY_ROOT}), body


def test_russian_imperative_is_an_order_but_past_tense_is_not():
    assert ap.detected_authority("Выполните `sudo systemctl restart aicc`.") == frozenset(
        {ap.AUTHORITY_ROOT}
    )
    assert ap.detected_authority("Агент выполнял `sudo systemctl restart aicc`.") == frozenset()


def test_a_command_alone_on_its_line_is_an_order():
    """A fenced block, a bullet, or a shell prompt: the line carries the
    command and nothing else, so the task is showing what to run."""
    for body in (
        "Steps:\n```sh\nsudo apt-get install -y ripgrep\n```",
        "Steps:\n- sudo systemctl restart aicc-worker\n",
        "Steps:\n$ sudo -u postgres psql -c 'select 1'\n",
        "Steps:\n    sudo chown aicc /srv/aicc\n",
    ):
        assert ap.detected_authority(body), body


def test_suspicion_never_enters_required_or_missing():
    decision = ap.decide("t", _THIS_TASKS_OWN_BODY)
    assert decision.missing == frozenset()
    assert not decision.suspected & decision.required


def test_declared_field_parks_even_when_only_quoted_evidence_surrounds_it():
    """Declaration is authoritative and position-exempt: an author who says
    `Requires-Authority: root` is stating a fact about the work, not quoting."""
    decision = ap.decide("t", _THIS_TASKS_OWN_BODY + "\nRequires-Authority: root")
    assert not decision.ok
    assert decision.missing == frozenset({ap.AUTHORITY_ROOT})
    # The postgres mention stays a suspicion — declaring root does not
    # silently promote every quoted command in the same body.
    assert decision.suspected == frozenset({ap.POSTGRES_ROLE_PREFIX + "postgres"})


def test_a_match_inside_a_standalone_command_line_is_an_order():
    """`psql -U postgres ...` matches at its ARGUMENT, five characters into
    the line — the anchor is the command head, not the match offset."""
    assert ap.detected_authority("psql -U postgres -c 'select 1'") == frozenset(
        {ap.POSTGRES_ROLE_PREFIX + "postgres"}
    )
    # ...but the same argument reached through prose is not an order.
    assert ap.detected_authority("The log shows psql -U postgres failing.") == frozenset()
    assert ap.suspected_authority("The log shows psql -U postgres failing.") == frozenset(
        {ap.POSTGRES_ROLE_PREFIX + "postgres"}
    )


# --------------------------------------------------------------------------
# Routing: which executor, not merely whether the fleet grants it at all
# (acceptance 3).
# --------------------------------------------------------------------------


def test_every_routed_executor_has_an_authority_entry():
    """An executor absent from the table grants nothing — safe, but silent.
    Keep the table and the routing matrix in step so a new lane is a decision
    rather than an omission."""
    from command_center.orchestrator.routing import ROUTING_MATRIX

    routed = {link["executor"] for cascade in ROUTING_MATRIX.values() for link in cascade}
    assert routed <= set(ap.EXECUTOR_AUTHORITY)


def test_no_executor_grants_anything_today():
    assert all(grants == frozenset() for grants in ap.EXECUTOR_AUTHORITY.values())
    assert ap.FLEET_GRANTED_AUTHORITY == frozenset()


def test_a_requirement_names_the_executors_that_can_serve_it(monkeypatch):
    monkeypatch.setitem(ap.EXECUTOR_AUTHORITY, "codex", frozenset({ap.AUTHORITY_ROOT}))
    assert ap.executors_granting({ap.AUTHORITY_ROOT}) == ("codex",)
    # An empty requirement is served by every executor: an ordinary task
    # routes exactly as it did before this module existed.
    assert ap.executors_granting(frozenset()) == tuple(sorted(ap.EXECUTOR_AUTHORITY))


def test_an_unknown_executor_grants_nothing():
    assert ap.executor_authority("nonesuch") == frozenset()


def test_authority_split_across_two_lanes_is_not_satisfiable(monkeypatch):
    """One task runs on ONE executor. Answering "satisfiable" from the fleet-
    wide union would be wrong the moment two lanes hold different privileges:
    the cascade would narrow to nothing and the payload would be built for a
    run that cannot happen."""
    monkeypatch.setitem(ap.EXECUTOR_AUTHORITY, "claude", frozenset({ap.AUTHORITY_ROOT}))
    monkeypatch.setitem(
        ap.EXECUTOR_AUTHORITY, "codex", frozenset({ap.POSTGRES_ROLE_PREFIX + "postgres"})
    )
    monkeypatch.setattr(
        ap,
        "FLEET_GRANTED_AUTHORITY",
        frozenset({ap.AUTHORITY_ROOT, ap.POSTGRES_ROLE_PREFIX + "postgres"}),
    )
    decision = ap.decide("t", "Requires-Authority: root, postgres")
    assert not decision.ok
    assert decision.capable_executors == ()
    # Nothing is "missing" -- each tag is granted somewhere -- so the reason
    # has to say the real problem, not print an empty set.
    assert decision.missing == frozenset()
    assert decision.reason == (
        "requires_privileged_authority: no_single_executor_grants: "
        "postgres_role:postgres,root"
    )
    assert not decision.reason.startswith("cascade_exhausted:")


def test_one_lane_holding_both_is_satisfiable(monkeypatch):
    monkeypatch.setitem(
        ap.EXECUTOR_AUTHORITY,
        "codex",
        frozenset({ap.AUTHORITY_ROOT, ap.POSTGRES_ROLE_PREFIX + "postgres"}),
    )
    monkeypatch.setattr(
        ap,
        "FLEET_GRANTED_AUTHORITY",
        frozenset({ap.AUTHORITY_ROOT, ap.POSTGRES_ROLE_PREFIX + "postgres"}),
    )
    decision = ap.decide("t", "Requires-Authority: root, postgres")
    assert decision.ok
    assert decision.capable_executors == ("codex",)
    assert decision.reason is None
