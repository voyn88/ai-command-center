#!/usr/bin/env python3
"""CLI for importing a task package into `data/tasks.json`.

Thin wrapper around `command_center.task_import` — the exact same
parse/validate/preview/apply pipeline the Create Task page's "Импорт пакета
задач" uploader uses, so a package validated here behaves identically when
uploaded through the UI (and vice versa).

The package may be JSON, YAML, Markdown or plain text (see
`command_center.task_import`'s module docstring); the format is inferred from
the file extension, overridable with --format.

Usage:
    python scripts/import_tasks.py PACKAGE.(json|yaml|md|txt) --dry-run
        Parses and validates the package, prints the preview (new/duplicate
        counts, errors, warnings, per-task table). Never writes to disk.

    python scripts/import_tasks.py PACKAGE.(json|yaml|md|txt) --apply
        Same validation, then commits the new tasks — one locked write per
        task, not a single transaction, so an import that fails part-way
        leaves the tasks it already wrote in the store (the error message
        lists them). Refuses to run (exit code 2) if the package has any
        blocking validation error, including a `depends_on` id that resolves to
        neither the package nor the current store (the default policy —
        see --allow-unresolved-dependencies).

Exactly one of --dry-run/--apply is required, so a bare invocation can never
accidentally write.

By default, an unresolved dependency (a `depends_on` id absent from both the
package and `data/tasks.json`) blocks the whole package — pass
--allow-unresolved-dependencies to explicitly opt into the old, permissive
behavior (downgrades it to a warning; import proceeds). Never the default:
a typo'd dependency id must never import silently.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from command_center import task_import  # noqa: E402


def _print_preview(preview: task_import.ImportPreview) -> None:
    print(f"Package id:      {preview.package_id}")
    print(f"Schema version:  {preview.schema_version}")
    print(f"Package hash:    {preview.package_hash}")
    print(f"Total tasks:     {preview.total_tasks}")
    print(f"New:             {len(preview.new_items)}")
    print(f"Duplicates:      {len(preview.duplicate_ids)}")
    print(f"Errors:          {len(preview.errors)}")
    print(f"Warnings:        {len(preview.warnings)}")
    print()

    if preview.rows:
        print(f"{'ID':<24} {'OUTCOME':<10} {'PROJECT':<10} {'STATUS':<14} {'PRIORITY':<10} TITLE")
        for row in preview.rows:
            print(f"{row.id:<24} {row.outcome:<10} {row.project:<10} {row.status:<14} {row.priority:<10} {row.title[:60]}")
        print()

    for issue in preview.errors:
        print(f"ERROR   [{issue.task_ref or '—'}] {issue.message}")
    for issue in preview.warnings:
        print(f"WARNING [{issue.task_ref or '—'}] {issue.message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a JSON/YAML/Markdown/text task package into data/tasks.json")
    parser.add_argument("package", type=Path, help="Path to the task package file (.json/.yaml/.yml/.md/.txt)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Validate and preview only; write nothing")
    mode.add_argument("--apply", action="store_true", help="Validate and commit the new tasks in one write")
    parser.add_argument(
        "--format",
        choices=["json", "yaml", "markdown", "text"],
        default=None,
        help="Override the format instead of inferring it from the file extension",
    )
    parser.add_argument(
        "--allow-unresolved-dependencies",
        action="store_true",
        help="Opt-in: downgrade an unresolved depends_on id to a warning instead of a blocking error",
    )
    args = parser.parse_args(argv)

    if not args.package.exists():
        print(f"Файл не найден: {args.package}", file=sys.stderr)
        return 2

    try:
        parsed = task_import.parse_task_package(
            args.package.read_text(encoding="utf-8"), filename=str(args.package), fmt=args.format
        )
    except task_import.TaskImportError as exc:
        print(f"Ошибка разбора пакета: {exc}", file=sys.stderr)
        return 2

    validation = task_import.validate_task_package(parsed)
    preview = task_import.build_import_preview(
        ROOT, parsed, validation, allow_unresolved_dependencies=args.allow_unresolved_dependencies
    )
    _print_preview(preview)

    if preview.has_blocking_errors:
        print("Пакет содержит ошибки валидации — импорт невозможен.", file=sys.stderr)
        return 2

    if args.dry_run:
        return 0

    try:
        result = task_import.apply_task_package(
            ROOT, parsed, validation, allow_unresolved_dependencies=args.allow_unresolved_dependencies
        )
    except task_import.TaskImportError as exc:
        print(f"Ошибка применения пакета: {exc}", file=sys.stderr)
        return 2

    print(f"Импортировано: {len(result.imported_ids)} — {result.imported_ids}")
    print(f"Пропущено (дубликаты): {len(result.skipped_duplicate_ids)} — {result.skipped_duplicate_ids}")
    for warning in result.warnings:
        print(f"WARNING [{warning.task_ref or '—'}] {warning.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
