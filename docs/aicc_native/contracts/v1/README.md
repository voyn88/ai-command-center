# AICC Native read-only contract v1

All responses have `schema_version`, `snapshot_revision`, `generated_at`, `staleness`, `trace_id` and `data`. The gateway returns only redacted, allowlisted DTOs.

| Resource | Semantics |
| --- | --- |
| `GET /v1/snapshot` | Atomic dashboard snapshot; supports `If-None-Match` / revision negotiation. |
| `GET /v1/tasks` and `/v1/tasks/{id}` | Cursor-paginated task evidence; status is server-derived. |
| `GET /v1/runs/{id}/timeline` | Bounded, typed, redacted events only. |
| `GET /v1/events?after_cursor=` | Resumable stream. A compacted/gapped cursor yields `resync_required`. |

The future `POST /v1/commands` is deliberately out of v1 read scope. It must require an idempotency key, authorization, policy decision, confirmation and durable audit record.
