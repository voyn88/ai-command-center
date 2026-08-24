# AICC Native vertical slice plan

Task: `NEW-1293` — Phase 0/1 fixture-first foundation.

## Ownership

| Area | Owner | Paths |
| --- | --- | --- |
| Product/visual | Product UX | `docs/aicc_native/prototype/`, `docs/aicc_native/PRODUCT_BLUEPRINT.md` |
| Native runtime | Client architecture | `clients/aicc-native/`, build and test configuration |
| AIOS contract | Integration | `docs/aicc_native/contracts/`, fixture schemas |

## Ordered delivery

1. Scaffold the client domain as a pure fixture-driven layer; no network, secrets or AIOS code.
2. Model Overview, Tasks, Agents/Workers, Delivery Pipeline and Activity as explicit states, including `UNKNOWN`, offline and degraded conditions.
3. Add deterministic fixture decoding, evidence-status derivation and redaction guard tests.
4. Add a native presentation spike only after the core contract decodes on the target toolchain.
5. Independently review exact commit, then publish through the guarded delivery pipeline.

## Acceptance for this slice

- The five screen models can render from versioned fixtures without a server.
- A task cannot be `completed` if acceptance, merged SHA or deployed SHA is absent.
- Offline and event-gap states remain explicit.
- A DTO containing raw credential/path/prompt-shaped fields is rejected at the boundary.
- No production control action is exposed.
