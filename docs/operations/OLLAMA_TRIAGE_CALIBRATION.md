# Local-model backlog triage: not yet fit for production use

Owner: `dimastov-lab` (AICC Cost thread, opened 2026-08-20). Goal: cut paid
token spend by moving as much triage work as possible onto a free local
model, without losing accuracy. This records two calibration attempts, why
both missed the owner's own promotion bar, and what is (and is not)
currently wired as a result.

`backlog_triage(p_task_id, p_decision, p_detail)`
(`command_center/db/sql/0008_backlog_triage.up.sql`) is the only decision
seam for turning an `UNTRIAGED` finding into `accept` → `OPEN`,
`refine` → `NEEDS_REFINEMENT`, `done` → `DONE`, or `duplicate` → `DECIDED`.
**As of this writing it is uncalled from any Python code** — no CLI
subcommand, no API route, no `backlog_client.py` wrapper; the only callers
are `tests/db/test_backlog_triage.py` and the grant assertion in
`tests/db/test_roles_render.py`. Today, triage happens only as a manual,
human act (the 2026-08-20 event `VOYN-W0-BACKLOG-RECONCILE-ALL` referenced in
`docs/operations/SCHEMA_VERSION_DRIFT.md`). Ollama itself is wired only as a
generic, read-only execution provider (`OllamaProvider`/`OllamaRuntime` in
`command_center/runtime/providers.py`) with no path into triage decisions.
So this document is a promotion gate for *future* wiring, not a rollback of
anything live.

## Infrastructure (as proven on worker-01)

- Ollama v0.32.14, user-space, formalized as a real systemd **user** unit
  `voyn-ollama.service` (not root, since the owner has no non-interactive
  sudo). The second calibration pass discovered a second, earlier install
  (`~/.local/ollama`, present since 2026-08-19) with `qwen2.5-coder:7b/14b`,
  `qwen3-vl:8b` and `bge-m3` already pulled — matching the owner's intended
  model-to-task routing matrix exactly. The duplicate `~/ollama-local`
  install made during the first pass was removed.
- worker-01 is CPU-only (no GPU) and has 30GB RAM; this work uses ~1GB of
  it. No dedicated server is needed for triage-scale classification. A
  separate box would only matter for local generation of real code by 30B+
  models, which is a distinct, not-recommended idea (quality risk on live
  code), not part of this task.
- control-01 (15GB RAM, hosts the live Postgres) was deliberately left
  untouched to avoid competing with production for memory.

## Attempt 1 (2026-08-20): `qwen2.5:7b-instruct`, 600-char truncated context

Sample: the same 22 real findings a live human triage of the 85 `UNTRIAGED`
backlog items produced that day.

- 14/22 (64%) exact match across all four decision classes.
- 14/18 (78%) if `done` is excluded.
- Owner's promotion bar: **≥90% holdout accuracy**. Not met.

Miss analysis:
- All 4 `done` cases were predicted `accept`. The PR link proving `done` was
  frequently past the 600-character truncated context window — plausibly
  fixable by giving the model the full record, not a model-capability
  problem.
- 4/18 `accept` cases were under-called as `refine` — a genuine nuance a 7B
  model doesn't reliably catch.

## Attempt 2 (2026-08-21): `qwen2.5-coder:14b`, full untruncated body

Run after the owner said "делай" (go ahead) once the systemd unit existed.
Sample: 22 findings (15 short, 7 long).

- **41% — worse than attempt 1**, despite a larger model and no truncation.

Root-cause: this was not a model error. It was the ground truth. 12 of the
13 misses were short, epic-level records (`VOYN-INFRA-WORKER-01` through
`-13`) that the model consistently called `accept`, and that the grader
(the owner) had labeled `refine` on a "short = refine" length heuristic. On
re-reading, those records carry a concrete topic and a clear technical goal
(sometimes an explicit `Acceptance:` line) — the model's call looks
defensible; the length heuristic does not.

**Conclusion: the `accept`/`refine` boundary is not defined precisely enough,
even for the human grader, to anchor a calibration.** A 90%-holdout bar
needs ground truth that doesn't move when re-read, and this axis doesn't
have that yet.

## Architectural conclusion (holds independent of the calibration numbers)

`done` should never be decided by LLM guessing in the first place — it is
already decided deterministically elsewhere (grepping merged-commit titles
for the task ID; see `VOYN-W0-BACKLOG-RECONCILE-ALL`). Separately, and for
an unrelated reason, `backlog_triage()`'s own DB seam already enforces this
structurally: a `done` decision requires matching `pr` and `sha` evidence
rows and is refused with `done_needs_evidence` otherwise (see
`0008_backlog_triage.up.sql` and `tests/db/test_backlog_triage.py`) — so even
a wired-in LLM could not rubber-stamp `done` on a bare claim. Any future
local-model input belongs only on the `accept`/`refine` split among findings
the deterministic check has *not* already resolved to `done`.

## Gate for future wiring

- Do not add a caller of `backlog_triage('done', ...)` driven by an LLM
  verdict; `done` stays on the deterministic commit-grep path.
- Do not wire Ollama (or any model) into the `accept`/`refine` decision
  until holdout accuracy against that decision reaches the owner's 90% bar,
  measured against ground truth two independent graders agree on — a
  single person's one-pass labels are exactly what attempt 2 showed isn't
  reliable enough on its own.
- If a future attempt clears the bar, land it as an advisory suggestion the
  human triage step reviews, not an autonomous call into
  `backlog_triage()`. No code currently calls that function at all, so
  promoting it means adding a new, explicit caller — not flipping a flag on
  an existing one.

## Recommendation for the next attempt

Stop calibrating against `accept`/`refine` — it is a matter of judgment, not
a checkable fact, and attempt 2 showed the "ground truth" itself isn't
stable under it. Calibrate instead against a class that has an objective
answer: near-duplicate detection over `UNTRIAGED` findings using `bge-m3`
embeddings (already pulled on worker-01, and exactly this model's role in
the owner's routing matrix). Embedding similarity plus a spot audit gives a
correctness check that doesn't depend on how carefully any one grader
labeled 22 rows by hand.
