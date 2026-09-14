"""Generate docs/architecture/README.md from the Mermaid sources in this repo.

The `.mmd` files in `docs/architecture/` (`context.mmd`, `container.mmd`) are the
single source of truth for the C4 diagrams. `README.md` in that directory is a
*generated* file: it embeds each `.mmd` verbatim in a fenced ```mermaid``` block
so GitHub renders it natively, plus hand-written prose around the diagrams.

Usage
-----
    python scripts/generate_architecture_docs.py          # regenerate README.md
    python scripts/generate_architecture_docs.py --check   # exit 1 if stale

`tests/architecture/test_architecture_docs_freshness.py` runs the `--check` mode
as part of the existing architecture-fitness pytest suite, so a `.mmd` edit that
was not followed by regeneration fails CI the same way any other fitness gate
does.

Each `.mmd` file is a plain Mermaid `flowchart`, not the `C4Context`/
`C4Container` diagram types — matching the existing precedent in
`ARCHITECTURE.md` section 1, whose context diagram already renders reliably on
GitHub. Mermaid's dedicated C4 syntax is newer and less consistently supported
by GitHub's bundled renderer.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_DIR = REPO_ROOT / "docs" / "architecture"
README_PATH = ARCHITECTURE_DIR / "README.md"

# Ordered (not alphabetical): context before container, matching C4 reading order.
DIAGRAMS = [
    {
        "key": "context",
        "file": "context.mmd",
        "title": "1. System context (C4 level 1)",
        "intro": (
            "Who and what AI Command Center talks to, from the outside. The system "
            "boundary is the repository this document lives in; everything else is "
            "an external person or system it integrates with."
        ),
    },
    {
        "key": "container",
        "file": "container.mmd",
        "title": "2. Containers (C4 level 2)",
        "intro": (
            "The deployable/runnable units inside the AI Command Center boundary, "
            "and how they reach the external systems from the context diagram. See "
            "[`ARCHITECTURE.md`](../../ARCHITECTURE.md) for the prose description "
            "these containers implement."
        ),
    },
]

HEADER = """\
<!-- GENERATED FILE — do not hand-edit.
     Source: the `.mmd` files in this directory.
     Regenerate with `python scripts/generate_architecture_docs.py`.
     Checked for staleness by tests/architecture/test_architecture_docs_freshness.py. -->

# AI Command Center — architecture as code

C4-style Mermaid diagrams for AI Command Center, checked in as text so they render
natively on GitHub and stay reviewable in a normal diff. These are diagrams, not
authority: [`ARCHITECTURE.md`](../../ARCHITECTURE.md) and the accepted
[ADRs](../adr/) are authoritative if a diagram and the prose ever disagree — update
the `.mmd` source and regenerate rather than editing this file.
"""

FOOTER = """\
## Keeping this in sync

Each diagram above is generated from the `.mmd` file of the same name in this
directory — edit that file, not this one. After editing, run:

```sh
python scripts/generate_architecture_docs.py
```

`tests/architecture/test_architecture_docs_freshness.py` (part of the repository's
existing architecture-fitness suite) fails if this file is stale relative to the
`.mmd` sources, so a diagram change without regeneration does not merge silently.

An ADR that changes AI Command Center's external integrations or its container
boundaries should update these diagrams — see
[`docs/adr/TEMPLATE.md`](../adr/TEMPLATE.md).
"""


def _diagram_section(diagram: dict) -> str:
    mmd_path = ARCHITECTURE_DIR / diagram["file"]
    body = mmd_path.read_text(encoding="utf-8").rstrip("\n")
    return (
        f"## {diagram['title']}\n\n"
        f"{diagram['intro']}\n\n"
        f"```mermaid\n{body}\n```\n\n"
        f"Source: [`{diagram['file']}`]({diagram['file']})\n"
    )


def render() -> str:
    sections = "\n".join(_diagram_section(diagram) for diagram in DIAGRAMS)
    return f"{HEADER}\n{sections}\n{FOOTER}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/architecture/README.md is stale, without writing it",
    )
    args = parser.parse_args(argv)

    rendered = render()

    if args.check:
        current = README_PATH.read_text(encoding="utf-8") if README_PATH.exists() else ""
        if current != rendered:
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=str(README_PATH),
                tofile="<generated>",
            )
            sys.stderr.write("docs/architecture/README.md is stale:\n")
            sys.stderr.writelines(diff)
            sys.stderr.write(
                "\nRun `python scripts/generate_architecture_docs.py` and commit the result.\n"
            )
            return 1
        print("docs/architecture/README.md is up to date.")
        return 0

    README_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {README_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
