# Sample backlog projection (synthetic)

This fixture reproduces every RECORD SHAPE observed in the canonical
Markdown backlog without reproducing its content: the canonical file names
internal projects, hosts and paths, and this repository is public, so a
snapshot of the real file must never be committed (recorded incident class).
The importer's run against the real file happens in the local-only test that
skips when the file is absent.

- **VOYN-W0-S1** | Wave 0 | OPEN | P0 | Platform | `first-task` | A plain wave-0 record.
  - Acceptance: sub-bullets travel into the body.
  - Target repo (owner decision): `~/somewhere/repo-a`.
- **VOYN-W0-S2** | Wave 0 | **P1 not a status** | Platform | `broken-status` | Status outside vocabulary.
- **VOYN-W0-S3** | Wave 0 | IN_PROGRESS (slice 1 DONE) | **P0 (annotated)** | Ops | `annotated` | Status and priority annotations normalize to exact tokens.
- **VOYN-W0-G1** | Wave 0 | OPEN | Product Owner | `gate` | A control record, not an executable task.
- **VOYN-W0.5-S1** | Wave 0.5 | READY_TO_REVIEW | Hardware | `wave-half` | Wave 0.5 is distinct from wave 0.
- **VOYN-COM-S1** | COM | OPEN | Legal | `lane-com` | A named parallel lane.
- **VOYN-POOL-S1** | W7 | OPEN | Product | `idea-pool` | W7 is an idea pool, distinct from wave 7.
- **VOYN-LANE-P1** | P1 | OPEN | UX | `lane-p1` | P1 in the lane slot is a lane, not a priority.
- **VOYN-W0-S4** | Wave 0 | UNTRIAGED | **P0** | Security | `untriaged-finding` | Non-executable status.
- **VOYN-W0-S5** | Wave 0 | DONE | Platform | `no-slug-follows` | Older format without priority.
- **VOYN-W0-S1** | Wave 0 | OPEN | P0 | Platform | `duplicate` | The first occurrence wins; this one is reported.
- **NOT-VOYN-1** | Wave 0 | OPEN | P0 | X | `bad-id` | Does not match the id shape at all (regex never captures it).
- **VOYN-BAD-WAVE** | Wave Q | OPEN | P0 | X | `bad-wave` | Wave does not normalize.
- **VOYN-W0-S6** | Wave 0 | DECIDED | P0 | Architecture | `decision-record` | An accepted architecture decision.
