# Decision Log

Program- and founder-level decisions. Architecture-level decisions live in `docs/adr/`.

| Id | Date | Decision | Record |
|---|---|---|---|
| DR-ROADMAP-AUTHORITY-001 | 2026-07-28 | Final goal, success measures, in-scope products, canonical project-id mapping, authority hierarchy, horizon boundaries, and disposition (accept/defer/reject) of candidate roadmap content — including the explicit non-approval of the `roadmap/program/` package. | [docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md](docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md) |
| DR-GITHUB-BRANCH-PROTECTION-001 | 2026-09-02 | `voyn88/ai-command-center` GitHub branch protection is unconfigured (0 required reviews, no required status checks, `enforce_admins=false`) and there is no plan/billing barrier to enabling it (repo is public; the audit's private/free-tier premise was wrong). Decision: `merge_once` (`command_center/orchestrator/review_merge.py`) is accepted as the sole real enforcement point today, documented explicitly rather than implied as GitHub-backed; enabling branch protection via `scripts/enable-branch-protection.sh` remains an open, zero-cost hardening step for an operator with repo admin access, not a blocker. | [docs/GOVERNANCE_GITHUB_BRANCH_PROTECTION.md](docs/GOVERNANCE_GITHUB_BRANCH_PROTECTION.md) |

The canonical project-id mapping stated in DR-ROADMAP-AUTHORITY-001 §4 now also has an
architecture-tier record: [ADR 0009](docs/adr/0009-canonical-project-registry-and-validating-task-import.md)
records the 9-id `PROJECT_IDS` registry, the `BANK`/`LEGAL` sensitive subset, the alias table's
case-and-whitespace-only folding rule, `normalize_project_id`'s fail-to-`None` contract, and the
rule that registry changes require a new ADR rather than a script workaround — closing
DR-ROADMAP-AUTHORITY-001 §8 F3.
