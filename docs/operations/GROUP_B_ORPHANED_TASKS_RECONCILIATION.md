# Group B orphaned-tasks reconciliation

Snapshot: 2026-09-06, canonical AICC `main`
`1efba23d8c51440e0cf45ddb24f922df902b5069`.

Scope: the 7 repo-bound tasks that inherited `IN_PROGRESS` from the
markdown-backlog import of 2026-08-19 without an attached `work_item`:
`VOYN-W0-AICC-SRV-01`, `-01B`, `-05`, `-09`, `-CI-IMPACT-SERIAL`,
`-FLAKY-CTRLC`, and `VOYN-W0-PLAT-07`.

**This document is evidence and a proposed verdict per row, not an
executed transition.** Per this task's own scope note ("Depends on: owner
decision on which of these are actually still needed"), and per this
repo's [Wave 2 reconciliation](WAVE2_SAFE_DELIVERY_RECONCILIATION.md)
precedent, closing a task is the state machine's job
(`backlog_transition`/`backlog_dispatch`, `command_center/db/backlog_store.py`
/ `planner.py`), which runs under the `aicc_app` planner role — a role this
task-execution context does not hold and should not assume (see the `aicc_worker`
grant matrix in `docs/postgres-foundation.md`: execution hosts are
deliberately the narrowest-privileged role and do not dispatch or close
backlog rows themselves). The verdicts below are the reconciliation the
owner (or the planner, once briefed) needs to action each row.

Evidence method: `git log --all --grep` for the literal ID across every
branch and tag in this clone, cross-checked against `docs/`, `CHANGELOG.md`,
and `roadmap/`/`projects/` for any non-commit trace. No live database
connection was available or used from this task-execution context
(`AICC_PG_HOST` is unset here by design — see the role note above).

## Verdicts

| Task | Evidence found | Verdict | Rationale |
|---|---|---|---|
| `VOYN-W0-AICC-SRV-01` | No commit carries this exact literal ID. `docs/AIOS_BOUNDARY.md:318` names the lane `VOYN-W0-AICC-SRV-01..09` as accepted by the central backlog on **2026-08-19** — the same date as this group's markdown import, i.e. that date registered the lane, it did not start it. The substrate work is fully delivered under two sub-slice IDs: `VOYN-W0-AICC-SRV-01a` (PostgreSQL foundation — schema, roles, pooling, backup/restore; PR #284, 2026-08-13) and `VOYN-W0-AICC-SRV-01B` (all 33 tables mirrored across 16 PRs #285–#300, last 2026-08-14). `docs/postgres-foundation.md` describes exactly this split ("this slice (`SRV-01a`) delivers the substrate... until `SRV-01b` moves it onto this seam"). | **close-candidate (DONE, superseded by 01a+01B)** | Both halves of the substrate this task names are shipped and merged before the import date. Owner should confirm the bare `SRV-01` row was meant as the umbrella for 01a/01B (then close as DONE with those PRs as evidence) rather than a distinct, still-open scope — no distinct scope is evidenced anywhere in the repo. |
| `VOYN-W0-AICC-SRV-01B` | Literal ID on 16 merged PRs (#285–#300, 2026-08-13→08-14). The last, PR #300 (`b7bc3006`), states explicitly: "the proposal family — all 33 tables mirrored." No further slices or remediation follow-ups exist under this ID after that. | **close-candidate (DONE)** | Delivery is complete and self-declared finished by its own final commit message; nothing points to open work after slice 15. |
| `VOYN-W0-AICC-SRV-05` | Literal ID on the original delivery (`#322` slice 1, `#323` slice 2, both 2026-08-19 — the import date itself) **and** two later remediation deliveries under review-retry IDs: `SRV-05-C-RETRY` (#577) and `SRV-05-DESIGN-RETRY-REM-REM-REM` (#563), both merged **2026-09-05** — yesterday relative to this snapshot. The final REM commit message shows an active review loop (PR #539 rejected the prior attempt on two findings; #563 fixes both). | **keep active — repair the link, do not close** | This is not idle: real work under this exact ID chain landed as recently as one day before this snapshot, going through the normal review-reject-remediate cycle the state machine already models. The `IN_PROGRESS`-without-`work_item` state is very likely a stale link on the *original* backlog row left over from before the remediation chain existed, not an abandoned task. Recommend re-pointing the row's `work_item` at the current remediation lineage (PR #563) rather than transitioning it to DONE or closing it. |
| `VOYN-W0-AICC-SRV-09` | Literal ID on the original delivery (`#318`, 2026-08-19, "a run is finalized when its report is durable, not when its state turns terminal") and one remediation delivery, `SRV-09-FINALIZED-AT-REM-CANCEL-DURABILITY` (#473, 2026-08-30, "autonomous delivery"). No further REM after 08-30 (7 days quiet as of this snapshot, vs. SRV-05's 1 day). | **close-candidate (DONE)**, evidence PR #318 + #473 | The remediation chain terminated without a further rejection/retry, unlike SRV-05's still-active loop. Owner should confirm no open review finding remains before closing; if confirmed, transition to DONE with #473 as the closing evidence. |
| `VOYN-W0-AICC-CI-IMPACT-SERIAL` | One self-contained delivery, literal ID, PR #379 (2026-08-24): "preserve serial split in impact precheck" + test coverage + an acceptance-gate fix. No follow-up remediation IDs exist. | **close-candidate (DONE)**, evidence PR #379 | Single-PR delivery with no rejection/retry trail — matches the shape of a task that finished cleanly on its first pass. |
| `VOYN-W0-AICC-FLAKY-CTRLC` | **Zero matches** anywhere: no commit (any branch), no doc, no test file references this literal ID. The only topically-adjacent commit, `225efc70` "de-flake the Ctrl+C orphan-cleanup process-tree test" (2026-07-29), predates the 2026-08-19 import by three weeks and carries no VOYN task tag — it is very likely unrelated prior work, not this task under a different name. | **needs owner decision — no work_item ever existed** | This is the one row in the group with no dispatch trace of any kind. Owner should say whether the underlying flake still reproduces (then dispatch fresh) or was incidentally fixed by unrelated CI/test work since import (then close as stale, citing the specific fix if one is found). |
| `VOYN-W0-PLAT-07` | **Zero matches** anywhere in this repository's code, docs, or full git history. Note the prefix: `PLAT` rather than `AICC` — every other task in this group and everything found in `docs/AIOS_BOUNDARY.md`'s SRV-lane note uses the `AICC` prefix for this repo's own backlog. | **needs owner decision — possibly mis-scoped to this repo** | No evidence this task was ever repo-bound to AICC at all; it may belong to a different repository's backlog (the import may have mis-tagged it repo-bound) or may be legitimately unstarted platform work. Owner should confirm which repository owns this task before any dispatch happens here. |

## Summary for the owner

- **Recommend closing DONE** (evidence attached above, no open remediation
  trail): `VOYN-W0-AICC-SRV-01` (as superseded by 01a/01B), `VOYN-W0-AICC-SRV-01B`,
  `VOYN-W0-AICC-SRV-09`, `VOYN-W0-AICC-CI-IMPACT-SERIAL`.
- **Recommend keeping active, repair the `work_item` link only**:
  `VOYN-W0-AICC-SRV-05` — real work landed under this ID as recently as
  2026-09-05.
- **Needs an owner call, no delivery evidence exists**:
  `VOYN-W0-AICC-FLAKY-CTRLC` (reproduce-or-close), `VOYN-W0-PLAT-07`
  (confirm repo ownership before dispatch).

None of the four "close DONE" candidates were closed as part of producing
this document — that transition, and the `work_item` repair for `SRV-05`,
still need to run through `backlog_transition`/`backlog_dispatch` under the
planner's own role once the owner signs off on the verdicts above.
