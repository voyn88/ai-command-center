# Decision Log

Program- and founder-level decisions. Architecture-level decisions live in `docs/adr/`.

| Id | Date | Decision | Record |
|---|---|---|---|
| DR-ROADMAP-AUTHORITY-001 | 2026-07-28 | Final goal, success measures, in-scope products, canonical project-id mapping, authority hierarchy, horizon boundaries, and disposition (accept/defer/reject) of candidate roadmap content — including the explicit non-approval of the `roadmap/program/` package. | [docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md](docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md) |
| DR-GITHUB-MERGE-ENFORCEMENT-001 | 2026-08-31 | Accepted risk on GitHub's unverified merge-time enforcement for `main`: the real gate is application code (`merge_once`/`_pr_is_mergeable`), not a GitHub plan/tier setting; no plan/org change is presupposed; parallel autonomous worker-lane count stays at its current 2-lane ceiling (now CI-enforced) until a full protection+ruleset+bypass-actor audit runs or `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships. | [docs/operations/GITHUB_MERGE_ENFORCEMENT_DECISION.md](docs/operations/GITHUB_MERGE_ENFORCEMENT_DECISION.md) |

The canonical project-id mapping stated in DR-ROADMAP-AUTHORITY-001 §4 now also has an
architecture-tier record: [ADR 0009](docs/adr/0009-canonical-project-registry-and-validating-task-import.md)
records the 9-id `PROJECT_IDS` registry, the `BANK`/`LEGAL` sensitive subset, the alias table's
case-and-whitespace-only folding rule, `normalize_project_id`'s fail-to-`None` contract, and the
rule that registry changes require a new ADR rather than a script workaround — closing
DR-ROADMAP-AUTHORITY-001 §8 F3.
