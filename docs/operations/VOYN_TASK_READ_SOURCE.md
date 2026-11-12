# voyn-task as a read-only source of `backlog_task` (stage 3)

Command: `python -m command_center.db backlog-import-voyn-task [--parse-only]`.

* **Flag** `AICC_VOYN_TASK_SOURCE` (`1|true|yes|on`), default **off**. Off: the command prints
  "nothing read" and exits 0; no DSN, directory or `psql` is touched. Markdown path
  (`ops/aicc_backlog_publish.py`, `backlog-import`) is unchanged and stays authoritative
  until the reconciliation week ends.
* **Definitions**: `tasks/*.yaml` (`VOYN_TASKS_DIR`, default `./tasks`). **State/revision**:
  `voyn_coordination.task_state` via `psql`, role `voyn_task_reader`; DSN only from
  `VOYN_TASK_READER_DSN` (else `VOYN_TASK_DSN`); a URL password is moved to `PGPASSWORD`.
* **Read only**: a single `SELECT`; no claim/transition/register. Writes go only to AICC's own
  `backlog_task` through the existing `BacklogStore` path.

| voyn-task | `backlog_task.status` |
|---|---|
| OPEN / IN_PROGRESS / READY_TO_REVIEW / DONE | same |
| BLOCKED | NEEDS_REFINEMENT (non-executable, no auto-resume; body keeps `[voyn-task status: BLOCKED]`) — proposal, owner decides |

UNTRIAGED, DEFER_TO_USER, SPLIT, DECIDED are AICC triage states and are never produced.
Fields: `repo` uses the Markdown path's rule (`infer_repo`: `Target repo` hint in the text, else the
task-id family); the coarse YAML `project` is NOT used (it disagreed on 470 of 640 real tasks);
`gate: true` or id `-G<n>` → `kind=gate`; `why`+`acceptance` → body. DB status wins; a task
without a `task_state` row takes the YAML status and is listed as UNREGISTERED.
Known difference: Markdown `title` is the slug, YAML `title` is the Russian heading (not compared).

## Existing rows are never rewritten
`backlog_upsert_task` sets status directly (bypassing the state machine) and replaces repo/wave/title/body,
so the adapter only inserts tasks absent from `backlog_task`. For existing rows it prints
`DIFF <id> <field>: aicc=… voyn-task=… (not written)` for status, repo, wave, priority, kind.
Dependencies and evidence are additive only (a change or removal in YAML is not reflected).
`pr` evidence is recorded as `pr` only when it is a full GitHub PR URL (migration 0030 clears
anything else); a bare `#229` is stored as acceptance evidence `pr: #229`.
`psql` runs with a scrubbed environment (no inherited `PG*`), `PGOPTIONS=-c default_transaction_read_only=on`,
and a password in any DSN form (URL, `?password=`, key=value) goes to `PGPASSWORD`.
CI subset: `tests/fixtures/voyn_task_source/real_slice.json` — id, project, status, wave, depends_on, gate
of all 647 real tasks (no titles or text).
