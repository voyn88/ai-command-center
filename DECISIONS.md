# Decision Log

Program- and founder-level decisions. Architecture-level decisions live in `docs/adr/`.

| Id | Date | Decision | Record |
|---|---|---|---|
| DR-ROADMAP-AUTHORITY-001 | 2026-07-28 | Final goal, success measures, in-scope products, canonical project-id mapping, authority hierarchy, horizon boundaries, and disposition (accept/defer/reject) of candidate roadmap content — including the explicit non-approval of the `roadmap/program/` package. | [docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md](docs/roadmap/FINAL_GOAL_AND_ROADMAP_AUTHORITY.md) |
| DR-SRV07-VOLUME-EXTRAPOLATION-001 | 2026-08-27 | The ~1.22M-row / ~6.5-minute backfill figures floated for the SQLite→PostgreSQL data migration are an extrapolation from one table's ratio in a 35 MB synthetic fixture (200,000 `message` rows, 50 `contact` rows), not a measurement of production volume — the live database's sixteen domain tables are currently empty (137 rows total). `VOYN-W0-AICC-SRV-07` and any dependent migration-window decision (e.g. `VOYN-W0-AICC-SRV-09`) must label these figures as extrapolation until the importer runs against real production data. | [docs/postgres-foundation.md](docs/postgres-foundation.md) |

The canonical project-id mapping stated in DR-ROADMAP-AUTHORITY-001 §4 now also has an
architecture-tier record: [ADR 0009](docs/adr/0009-canonical-project-registry-and-validating-task-import.md)
records the 9-id `PROJECT_IDS` registry, the `BANK`/`LEGAL` sensitive subset, the alias table's
case-and-whitespace-only folding rule, `normalize_project_id`'s fail-to-`None` contract, and the
rule that registry changes require a new ADR rather than a script workaround — closing
DR-ROADMAP-AUTHORITY-001 §8 F3.
