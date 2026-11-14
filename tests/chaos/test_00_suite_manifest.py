"""VOYN-W0-AICC-CHAOS-CONCURRENCY-SUITE: the review -> adjudicate -> merge
owner journeys, collected as one explicit, by-name suite instead of left
scattered across tests/db/test_review_merge.py (claim 34).

Each scenario lives in its own `test_scenario_<n>_<name>.py` file so the set
is recognizable by filename alone, sorted in dependency order (scenarios 5-7
build the reject -> remediation -> accept -> merge chain scenarios 1-3
establish the pieces of):

  1. accept_then_merge                        -- the happy path baseline
  2. ci_pending_blocks_merge                   -- a running check is not green
  3. ci_failed_bounded_rerun_stays_blocked     -- a real red change reruns once, then stays blocked
  4. ci_pending_failed_rerun_recovers          -- *new*: pending -> failed -> rerun -> green, one chain
  5. reject_spawns_remediation                 -- a REJECT dispatches a linked follow-up task
  6. remediation_chain_depth_limit             -- the follow-up chain is bounded, not eternal
  7. reject_remediation_accept_merge_e2e       -- *new*: the full chain, end to end
  8. external_merge_without_acceptance         -- a concurrent out-of-band merge is never silently DONE

Scenarios 4 and 7 are the two this suite was created to close: prior
coverage exercised every state individually, but no single test drove a PR
through pending -> failed -> rerun CI, and no single test drove a task
through the complete reject -> remediation -> accept -> merge chain. The
E2E chain additionally stands in for the live confirmation that
VOYN-W0-AICC-REVIEW-STUCK-ON-TRANSIENT-FAILURE currently blocks -- this
gives deterministic coverage of the whole path instead of none while that
is unresolved.

This test only checks the manifest above against the files actually on
disk, so a scenario renamed or removed without updating the list here (or
vice versa) fails loudly instead of silently drifting the suite out of
sync with its own claimed coverage. It needs no database and never skips.
"""

from __future__ import annotations

from pathlib import Path

_SCENARIOS = {
    1: "accept_then_merge",
    2: "ci_pending_blocks_merge",
    3: "ci_failed_bounded_rerun_stays_blocked",
    4: "ci_pending_failed_rerun_recovers",
    5: "reject_spawns_remediation",
    6: "remediation_chain_depth_limit",
    7: "reject_remediation_accept_merge_e2e",
    8: "external_merge_without_acceptance",
}


def test_all_eight_scenarios_are_present_as_one_named_set():
    here = Path(__file__).parent
    for number, name in _SCENARIOS.items():
        expected = here / f"test_scenario_{number}_{name}.py"
        assert expected.is_file(), f"missing chaos scenario file: {expected.name}"


def test_no_extra_or_unlisted_scenario_files_have_crept_in():
    here = Path(__file__).parent
    on_disk = {p.name for p in here.glob("test_scenario_*.py")}
    manifest = {
        f"test_scenario_{number}_{name}.py" for number, name in _SCENARIOS.items()
    }
    assert on_disk == manifest
