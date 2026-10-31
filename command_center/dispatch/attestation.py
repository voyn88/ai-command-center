"""Pure certification gate: may an agent be dispatched into the critical zone?

VOYN-AGT-ATTEST: a task in the critical zone (``priority == "Critical"``, see
``dispatch.models.CRITICAL_ZONE_PRIORITY``) must never reach an agent that has
not been certified through the evidence this module evaluates —
``AttestationRecord.test_results`` (the agent's certification test-case suite)
and ``AttestationRecord.critical_incidents`` (its historical record). Both are
recorded by an operator/CI job through ``attestation_config.save_record`` and
read back here as plain evidence; this module never touches storage, so the
decision itself is hermetic and total — the same shape as ``delivery_gate``
and ``capabilities.decide()``.

Fail-closed by construction: a missing record, an empty test suite, any
failing test case, or any historical critical incident all withhold
certification. There is no code path that certifies on partial evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# Typed reason codes (never a free-form string, mirrors dispatch.models)
# --------------------------------------------------------------------------

NO_RECORD = "no_attestation_record"
NO_TEST_CASES = "no_test_cases_recorded"
FAILING_TEST_CASES = "failing_test_cases"
HISTORICAL_INCIDENT = "historical_critical_incident"


@dataclass(frozen=True)
class CertificationCase:
    """One certification test-case outcome."""

    name: str
    passed: bool

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed}

    @classmethod
    def from_dict(cls, data: object) -> "CertificationCase | None":
        """`None` on anything malformed, so a garbage entry is dropped rather
        than silently counted as a passing (or even a named) test case."""
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        return cls(name=name, passed=data.get("passed") is True)


@dataclass(frozen=True)
class AttestationRecord:
    """One agent's certification evidence, as recorded by an operator/CI run."""

    agent_id: str
    test_results: tuple[CertificationCase, ...] = ()
    critical_incidents: int = 0
    recorded_at: str | None = None
    recorded_by: str | None = None

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "test_results": [t.as_dict() for t in self.test_results],
            "critical_incidents": self.critical_incidents,
            "recorded_at": self.recorded_at,
            "recorded_by": self.recorded_by,
        }

    @classmethod
    def from_dict(cls, agent_id: str, data: object) -> "AttestationRecord":
        """Fail-closed: a malformed field falls back to a value that keeps the
        record uncertifiable rather than one that widens the gate."""
        if not isinstance(data, dict):
            return cls(agent_id=agent_id)
        raw_results = data.get("test_results")
        results = (
            tuple(
                r
                for r in (CertificationCase.from_dict(item) for item in raw_results)
                if r is not None
            )
            if isinstance(raw_results, list)
            else ()
        )
        incidents = data.get("critical_incidents")
        critical_incidents = (
            incidents
            if isinstance(incidents, int)
            and not isinstance(incidents, bool)
            and incidents >= 0
            else 0
        )
        return cls(
            agent_id=agent_id,
            test_results=results,
            critical_incidents=critical_incidents,
            recorded_at=data.get("recorded_at"),
            recorded_by=data.get("recorded_by"),
        )


@dataclass(frozen=True)
class AttestationDecision:
    """Whether an agent is certified, and why not when it is refused. An empty
    `reasons` tuple is exactly the certified case — the same "reasons imply
    refusal" contract as `delivery_gate.DeliveryDecision`."""

    certified: bool
    reasons: tuple[str, ...]


def evaluate_attestation(record: AttestationRecord | None) -> AttestationDecision:
    """Decide whether `record`'s evidence certifies its agent for the critical
    zone. Total and pure: every branch returns, nothing raises, nothing is
    read from disk or the network.

    Certified requires all of: a record exists, it carries at least one
    certification test case, every recorded test case passed, and it carries
    zero historical critical incidents. Any one gap withholds certification.
    """
    if record is None:
        return AttestationDecision(certified=False, reasons=(NO_RECORD,))

    reasons: list[str] = []
    if not record.test_results:
        reasons.append(NO_TEST_CASES)
    else:
        failed = sorted(t.name for t in record.test_results if not t.passed)
        if failed:
            reasons.append(f"{FAILING_TEST_CASES}:{','.join(failed)}")
    if record.critical_incidents > 0:
        reasons.append(HISTORICAL_INCIDENT)

    return AttestationDecision(certified=not reasons, reasons=tuple(reasons))
