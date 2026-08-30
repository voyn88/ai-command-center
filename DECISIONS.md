# Decision Log

Program- and founder-level decisions. Architecture-level decisions live in `docs/adr/`.

| Id | Date | Decision | Record |
|---|---|---|---|
| DR-ROADMAP-AUTHORITY-001 | 2026-07-28 | Final goal, success measures, in-scope products, canonical project-id mapping, authority hierarchy, horizon boundaries, and disposition (accept/defer/reject) of candidate roadmap content — including the explicit non-approval of the `roadmap/program/` package. | [docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md](docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md) |
| DR-GITHUB-TIER-ENFORCEMENT-001 | 2026-08-26 | **Pending founder decision.** Of the three branch-protection fields queried on `main`, all three are absent/disabled (`required_approving_review_count=0`, no `required_status_checks`, `enforce_admins=false`); other protection fields (push restrictions, conversation resolution, signed commits, linear history, force-push/deletion controls) were not queried and are not claimed. `merge_once` app logic is the only confirmed gate on the reviewed/status-check axes. Choice needed: upgrade GitHub plan/org for real enforcement, or explicitly accept the gap with a hard cap on concurrent task agents until `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships. | [docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md](docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md) |

The canonical project-id mapping stated in DR-ROADMAP-AUTHORITY-001 §4 now also has an
architecture-tier record: [ADR 0009](docs/adr/0009-canonical-project-registry-and-validating-task-import.md)
records the 9-id `PROJECT_IDS` registry, the `BANK`/`LEGAL` sensitive subset, the alias table's
case-and-whitespace-only folding rule, `normalize_project_id`'s fail-to-`None` contract, and the
rule that registry changes require a new ADR rather than a script workaround — closing
DR-ROADMAP-AUTHORITY-001 §8 F3.
