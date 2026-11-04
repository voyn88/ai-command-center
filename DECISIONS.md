# Decision Log

Program- and founder-level decisions. Architecture-level decisions live in `docs/adr/`.

| Id | Date | Decision | Record |
|---|---|---|---|
| DR-ROADMAP-AUTHORITY-001 | 2026-07-28 | Final goal, success measures, in-scope products, canonical project-id mapping, authority hierarchy, horizon boundaries, and disposition (accept/defer/reject) of candidate roadmap content — including the explicit non-approval of the `roadmap/program/` package. | [docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md](docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md) |
| DR-GITHUB-BRANCH-PROTECTION-001 | 2026-11-01 | Live evidence (public REST, `docs/audits/github-branch-protection-2026-11-01/`) shows `voyn88/ai-command-center` is **public** and `main` already requires status checks `Final merge gate` + `Acceptance gate (independent verdict on exact SHA)` with `enforcement_level=everyone`. Decision: document a dual-layer model — GitHub required checks plus `merge_once` — rather than claiming protection is absent or that the gateway is the sole enforcement point. Authenticated-only fields (`required_approving_review_count`, `enforce_admins`) are left unverified; the stale enable script must not overwrite live contexts. Supersedes rejected PR #663. | [docs/GOVERNANCE_GITHUB_BRANCH_PROTECTION.md](docs/GOVERNANCE_GITHUB_BRANCH_PROTECTION.md) |

The canonical project-id mapping stated in DR-ROADMAP-AUTHORITY-001 §4 now also has an
architecture-tier record: [ADR 0009](docs/adr/0009-canonical-project-registry-and-validating-task-import.md)
records the 9-id `PROJECT_IDS` registry, the `BANK`/`LEGAL` sensitive subset, the alias table's
case-and-whitespace-only folding rule, `normalize_project_id`'s fail-to-`None` contract, and the
rule that registry changes require a new ADR rather than a script workaround — closing
DR-ROADMAP-AUTHORITY-001 §8 F3.
