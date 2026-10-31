"""VOYN-W0-AICC-ARCHITECTURE-AS-CODE: architecture diagrams stay generated.

``docs/architecture/context.mmd`` and ``container.mmd`` are the single source of
truth for AI Command Center's C4 diagrams; ``docs/architecture/README.md`` is a
generated file that embeds them so GitHub renders the diagrams natively. This
gate imports the generator (``scripts/generate_architecture_docs.py``) and
fails if the checked-in README no longer matches what the current ``.mmd``
sources would produce — the same "edit the source, forgot to regenerate"
failure mode the rest of ``tests/architecture`` guards other invariants
against.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_architecture_docs.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_architecture_docs", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_architecture_readme_matches_generated_output():
    generator = _load_generator()
    rendered = generator.render()
    current = generator.README_PATH.read_text(encoding="utf-8")
    assert current == rendered, (
        "docs/architecture/README.md is stale relative to the .mmd sources in "
        "that directory. Run `python scripts/generate_architecture_docs.py` "
        "and commit the result."
    )


def test_architecture_diagram_sources_are_nonempty():
    generator = _load_generator()
    for diagram in generator.DIAGRAMS:
        mmd_path = generator.ARCHITECTURE_DIR / diagram["file"]
        assert mmd_path.exists(), f"missing {mmd_path}"
        text = mmd_path.read_text(encoding="utf-8")
        assert text.strip(), f"{mmd_path} is empty"
        # Every C4 diagram here is authored as a Mermaid flowchart (see the
        # generator's module docstring for why C4Context/C4Container syntax is
        # avoided) — this is a smoke check for the diagram keyword, not a full
        # Mermaid grammar validation.
        assert "flowchart" in text, f"{mmd_path} does not declare a flowchart diagram"
