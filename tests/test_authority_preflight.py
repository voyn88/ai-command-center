"""The required-authority preflight (VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-
UNPRIVILEGED-EXECUTOR).

The incident: a task needing `sudo` / a host `postgres` role was dispatched
to the deliberately unprivileged worker principal, failed on `a password is
required`, and the cascade reproduced that refusal twice more --
`cascade_exhausted: task_status_failed`, three model runs, zero progress.

Three earlier designs answered it by SCANNING THE TASK PROMPT, and all three
were rejected in review for being wrong in one direction or the other. The
class those rejections belong to is pinned here as first-class regression
tests (`TestProseIsNeverScanned`): the module now decides from a DECLARATION,
so every one of those inputs must leave the decision untouched. If someone
ever reintroduces prompt sniffing, those tests fail immediately.
"""

from __future__ import annotations

import subprocess

import pytest

from command_center import authority_preflight as ap


class _FakeRunner:
    """A `subprocess.run` stand-in recording argv and replaying a verdict."""

    def __init__(self, returncode: int = 0, raises: BaseException | None = None) -> None:
        self.returncode = returncode
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, "", "")


# ---------------------------------------------------------------------------
# Declaration parsing
# ---------------------------------------------------------------------------


def test_absent_declaration_is_no_authority() -> None:
    """Every task that existed before this module declares nothing, and must
    keep behaving exactly as it did."""
    assert ap.normalize_authorities(None) == ()
    assert ap.normalize_authorities([]) == ()


def test_declaration_is_canonicalized_deduplicated_and_ordered() -> None:
    assert ap.normalize_authorities(["postgres_role", "ROOT", " root "]) == (
        "root",
        "postgres_role",
    )
    # Ordering follows AUTHORITY_ORDER, not the input and not the alphabet,
    # so the same set always serializes identically into a park reason.
    assert ap.normalize_authorities(
        ["external_credential", "root"]
    ) == ("root", "external_credential")


@pytest.mark.parametrize(
    "bad",
    [
        "root",  # a bare string is not a list, even though it is iterable
        {"root": True},
        ["root", 7],
        [None],
        42,
    ],
)
def test_a_malformed_declaration_fails_closed(bad) -> None:
    """Never "assume it needs nothing": that would dispatch a privileged task
    to an unprivileged executor while the author believed otherwise."""
    with pytest.raises(ap.AuthorityDeclarationError):
        ap.normalize_authorities(bad)


def test_an_unknown_authority_is_refused_not_ignored() -> None:
    with pytest.raises(ap.AuthorityDeclarationError, match="unknown authority"):
        ap.normalize_authorities(["kubernetes_admin"])


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_no_requirement_is_satisfied_by_no_grant() -> None:
    decision = ap.decide([], [])
    assert decision.ok and decision.missing == ()
    assert decision.reason_code == ap.REASON_SATISFIED


def test_a_held_authority_satisfies_the_requirement() -> None:
    assert ap.decide(["root"], ["root", "postgres_role"]).ok


def test_a_missing_authority_blocks_and_names_itself() -> None:
    decision = ap.decide(["root", "postgres_role"], ["root"])
    assert not decision.ok
    assert decision.missing == ("postgres_role",)
    assert decision.reason_code == ap.REASON_UNAVAILABLE
    # The reason is what the queue persists and what the backlog's park
    # classifier matches on (`cascade_exhausted: authority_unavailable%`),
    # so its prefix is a contract, not cosmetics.
    assert decision.reason.startswith("authority_unavailable:")
    assert "postgres_role" in decision.reason


def test_the_reason_string_is_stable_for_a_given_set() -> None:
    """Set arithmetic rendered through AUTHORITY_ORDER: the same requirement
    must produce the same bytes however the caller ordered its input."""
    a = ap.decide(["postgres_role", "root"], [])
    b = ap.decide(["root", "postgres_role"], [])
    assert a.reason == b.reason


# ---------------------------------------------------------------------------
# The regression pins for the three rejected prompt-scanning designs.
#
# Each input below broke one of them. Under a declaration-based design none
# of them can break anything, because none of them is ever read -- which is
# exactly the property these tests exist to keep.
# ---------------------------------------------------------------------------


class TestProseIsNeverScanned:
    #: Every one of these was a real review finding. Left column: what the
    #: text says. Right column: which rejected design it defeated.
    BODIES = [
        # PR #530: the verb allow-list let unlisted verbs through as "no
        # authority needed" and dispatched them into the failure loop.
        "Run `sudo cp /etc/x /etc/y` on the host.",
        "Run `sudo rm -rf /var/lib/thing`.",
        "Run `sudo bash -c 'systemctl restart aicc'`.",
        "Run `sudo python3 /opt/fix.py`.",
        # PR #530: context-free `-u postgres` parked work needing no host role.
        "docker run -u postgres --rm alpine true",
        # PR #571: negation was ignored, so a disclaimer parked the task.
        "This does not require root access.",
        "No sudo is needed; everything runs unprivileged.",
        # PR #571: command-shaped text in ordinary implementation work.
        "Add a test ensuring `sudo rm` is rejected by the validator.",
        "Document why users must not run `sudo apt` inside the container.",
        "Sanitize the string `su postgres` before logging it.",
        # PR #640: the guard window was a bag-of-words scan with no syntactic
        # link, so an unrelated earlier clause suppressed a real command.
        "Update the connection string, then run `sudo systemctl restart postgres`.",
        "To avoid manual steps, run `sudo apt install x`.",
        "If the fix is rejected, escalate and run `sudo systemctl restart nginx`.",
        # PR #640: long-form escalation the short-flag detector missed.
        "sudo --user=postgres psql -c 'select 1'",
        "sudo --user postgres psql -c 'select 1'",
    ]

    def test_no_body_can_change_a_decision(self) -> None:
        """`decide` takes no prompt at all -- the strongest possible form of
        this guarantee, checked at the signature."""
        for body in self.BODIES:
            assert ap.decide([], []).ok, body
            assert not ap.decide(["root"], []).ok, body

    def test_no_body_is_accepted_as_a_declaration(self) -> None:
        """A task body is not a declaration, however command-shaped it is.
        Each of these is a plain string, and a string is refused as a
        malformed declaration rather than parsed for authority names."""
        for body in self.BODIES:
            with pytest.raises(ap.AuthorityDeclarationError):
                ap.normalize_authorities(body)

    def test_no_body_satisfies_the_run_trailer_either(self) -> None:
        """The one text-reading function in the module reads a run's own
        anchored trailer, never a body. None of the prose above -- including
        the lines that are literally privileged commands -- produces one."""
        for body in self.BODIES:
            assert ap.declared_by_run(body) == (), body


# ---------------------------------------------------------------------------
# The run's own report
# ---------------------------------------------------------------------------


def test_the_trailer_is_read_when_emitted_as_its_own_line() -> None:
    text = "I cannot proceed.\nREQUIRES_AUTHORITY: root\nHEAD_SHA: abc1234\n"
    assert ap.declared_by_run(text) == ("root",)


def test_the_trailer_accepts_a_comma_list_and_unions_repeats() -> None:
    text = (
        "REQUIRES_AUTHORITY: postgres_role, root\n"
        "...later...\n"
        "REQUIRES_AUTHORITY: external_credential\n"
    )
    assert ap.declared_by_run(text) == (
        "root",
        "postgres_role",
        "external_credential",
    )


def test_an_inline_mention_is_not_a_trailer() -> None:
    """Anchored at both ends, exactly like `handlers._HEAD_SHA_TRAILER`: a
    sentence that merely talks about the contract does not invoke it."""
    assert ap.declared_by_run("the agent should emit REQUIRES_AUTHORITY: root here") == ()
    assert ap.declared_by_run("REQUIRES_AUTHORITY: root but only sometimes") == ()


def test_a_malformed_or_unknown_trailer_yields_nothing() -> None:
    """An executor's typo must not crash ingest or invent an authority; the
    task simply falls through to the outcome path it had before."""
    assert ap.declared_by_run("REQUIRES_AUTHORITY:\n") == ()
    assert ap.declared_by_run("REQUIRES_AUTHORITY: kubernetes_admin\n") == ()
    assert ap.declared_by_run("REQUIRES_AUTHORITY: root!\n") == ()
    assert ap.declared_by_run("") == ()
    assert ap.declared_by_run(None) == ()


def test_an_unknown_name_beside_a_known_one_keeps_the_known_one() -> None:
    assert ap.declared_by_run("REQUIRES_AUTHORITY: root, kubernetes_admin\n") == ("root",)


# ---------------------------------------------------------------------------
# Grants: deny by default, config narrowed by probe
# ---------------------------------------------------------------------------


def test_nothing_needed_means_nothing_probed() -> None:
    """The latency guarantee for every pre-existing task: a dispatch that
    declares nothing must not spawn a probe subprocess at all."""
    runner = _FakeRunner(returncode=0)
    assert ap.worker_granted_authorities(
        needed=(), env={ap.GRANT_ENV_VAR: "root"}, runner=runner
    ) == ()
    assert runner.calls == []


def test_an_undeclared_deployment_grants_nothing() -> None:
    """Deny by default -- the truth on every isolated worker (ADR-0010)."""
    runner = _FakeRunner(returncode=0)
    assert ap.worker_granted_authorities(needed=["root"], env={}, runner=runner) == ()
    # Not even probed: nothing was claimed, so there is nothing to verify.
    assert runner.calls == []


def test_a_declared_grant_must_still_pass_its_probe(monkeypatch) -> None:
    """Configuration can narrow the verifiable set, never widen it past
    reality: a stale `AICC_WORKER_AUTHORITIES=root` on a host that lost its
    sudoers entry must not re-create the incident."""
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/usr/bin/" + name)
    refused = _FakeRunner(returncode=1)
    assert (
        ap.worker_granted_authorities(
            needed=["root"], env={ap.GRANT_ENV_VAR: "root"}, runner=refused
        )
        == ()
    )
    assert refused.calls == [["sudo", "-n", "true"]], "the probe must be non-interactive"

    granted = _FakeRunner(returncode=0)
    assert ap.worker_granted_authorities(
        needed=["root"], env={ap.GRANT_ENV_VAR: "root"}, runner=granted
    ) == ("root",)


def test_a_missing_probe_binary_is_a_no(monkeypatch) -> None:
    monkeypatch.setattr(ap.shutil, "which", lambda name: None)
    runner = _FakeRunner(returncode=0)
    assert (
        ap.worker_granted_authorities(
            needed=["root", "postgres_role"],
            env={ap.GRANT_ENV_VAR: "root,postgres_role"},
            runner=runner,
        )
        == ()
    )
    assert runner.calls == []


@pytest.mark.parametrize(
    "boom",
    [
        OSError("no such binary"),
        subprocess.TimeoutExpired(cmd="sudo", timeout=5),
    ],
)
def test_a_probe_that_cannot_answer_is_a_no(monkeypatch, boom) -> None:
    """The question is whether the authority is usable right now, not why it
    is not. A hung or broken probe must never read as "granted"."""
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/usr/bin/" + name)
    runner = _FakeRunner(raises=boom)
    assert (
        ap.worker_granted_authorities(
            needed=["root"], env={ap.GRANT_ENV_VAR: "root"}, runner=runner
        )
        == ()
    )


def test_the_postgres_probe_never_waits_for_a_password(monkeypatch) -> None:
    """The incident's own command was interactive and hung until it failed;
    `-w` plus a bounded PGCONNECT_TIMEOUT is what keeps the probe cheap."""
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/usr/bin/" + name)
    seen: dict[str, object] = {}

    def runner(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs.get("env")
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert ap.probe_postgres_role(runner=runner, env={})
    assert "-w" in seen["argv"]
    assert seen["env"]["PGCONNECT_TIMEOUT"] == str(ap.PROBE_TIMEOUT_SECONDS)
    assert seen["timeout"] == ap.PROBE_TIMEOUT_SECONDS


def test_a_deployment_typo_is_dropped_not_raised(monkeypatch) -> None:
    """A malformed env entry must not take the worker off the fleet; dropping
    it fails in the safe direction (the task parks for the owner)."""
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/usr/bin/" + name)
    runner = _FakeRunner(returncode=0)
    assert ap.worker_granted_authorities(
        needed=["root"], env={ap.GRANT_ENV_VAR: "rooot, root"}, runner=runner
    ) == ("root",)


def test_only_the_needed_authorities_are_probed(monkeypatch) -> None:
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/usr/bin/" + name)
    runner = _FakeRunner(returncode=0)
    ap.worker_granted_authorities(
        needed=["root"],
        env={ap.GRANT_ENV_VAR: "root,postgres_role"},
        runner=runner,
    )
    assert runner.calls == [["sudo", "-n", "true"]]


def test_an_authority_with_no_probe_stands_on_the_declaration() -> None:
    """Stated, not hidden: there is no generic "do I hold some off-host
    credential" question, so that one is taken from configuration. Verify
    what can be verified; never invent verification that would be a guess."""
    runner = _FakeRunner(returncode=1)
    assert ap.worker_granted_authorities(
        needed=["external_credential"],
        env={ap.GRANT_ENV_VAR: "external_credential"},
        runner=runner,
    ) == ("external_credential",)
    assert runner.calls == []


# ---------------------------------------------------------------------------
# The composed call
# ---------------------------------------------------------------------------


def test_preflight_blocks_the_incident(monkeypatch) -> None:
    """The 2026-08-30 shape end to end: a task needing root and a host
    postgres role, on an unprivileged worker."""
    monkeypatch.setattr(ap.shutil, "which", lambda name: "/usr/bin/" + name)
    decision = ap.preflight(
        ["root", "postgres_role"], env={}, runner=_FakeRunner(returncode=1)
    )
    assert not decision.ok
    assert decision.missing == ("root", "postgres_role")


def test_preflight_passes_an_ordinary_task_without_touching_anything() -> None:
    runner = _FakeRunner(returncode=1)
    assert ap.preflight([], env={}, runner=runner).ok
    assert runner.calls == []
