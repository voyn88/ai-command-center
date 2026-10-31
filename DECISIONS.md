# Decision Log

Program- and founder-level decisions. Architecture-level decisions live in `docs/adr/`.

| Id | Date | Decision | Record |
|---|---|---|---|
| DR-ROADMAP-AUTHORITY-001 | 2026-07-28 | Final goal, success measures, in-scope products, canonical project-id mapping, authority hierarchy, horizon boundaries, and disposition (accept/defer/reject) of candidate roadmap content — including the explicit non-approval of the `roadmap/program/` package. | [docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md](docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md) |
| DR-GITHUB-TIER-ENFORCEMENT-001 | 2026-08-26 | **Pending founder decision.** `main`'s branch protection enforces nothing on the current plan (`required_approving_review_count=0`, no `required_status_checks`, `enforce_admins=false`); `merge_once` app logic is the only gate. Choice needed: upgrade GitHub plan/org for real enforcement, or explicitly accept the gap with a hard cap on concurrent task agents until `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships. | [docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md](docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md) |

DR-GITHUB-TIER-ENFORCEMENT-001 stays open until the founder picks Option A (change plan/org) or
Option B (accept the gap with an explicit cap on concurrent task agents until
`VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships). Its finding is machine-checkable in the meantime:
`python3 scripts/verify_branch_protection.py --repo voyn88/ai-command-center` reads the protection
endpoint and exits 0 only for what the API actually enforces — exit 2 means unverified, which is
not the same as unprotected. `tests/test_branch_protection_verifier.py` additionally fails the
suite if any Markdown document in the repository asserts branch protection as a working control
without qualifying it or citing this record.

The canonical project-id mapping stated in DR-ROADMAP-AUTHORITY-001 §4 now also has an
architecture-tier record: [ADR 0009](docs/adr/0009-canonical-project-registry-and-validating-task-import.md)
records the 9-id `PROJECT_IDS` registry, the `BANK`/`LEGAL` sensitive subset, the alias table's
case-and-whitespace-only folding rule, `normalize_project_id`'s fail-to-`None` contract, and the
rule that registry changes require a new ADR rather than a script workaround — closing
DR-ROADMAP-AUTHORITY-001 §8 F3.
