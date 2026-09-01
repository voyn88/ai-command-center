from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"

# The documents SRV-07/SRV-09 read for the source volume and the transfer plan.
MIGRATION_DOCS = (
    DOCS_DIR / "srv01b-schema-map.md",
    DOCS_DIR / "postgres-foundation.md",
)

# A paragraph is talking about *this* transfer's window only if it says
# "бэкфилл"/"backfill" — not the broader "окно"/"window"/"перенос", which also
# names ordinary things elsewhere in this repo (the SRV-05 payload's
# visibility window, a queue message transfer). Gating those on the word
# «экстраполяция» would force a real future measurement of one of them to
# call itself a guess — the mirror image of the error being fixed here.
_TRANSFER_WINDOW = re.compile(r"бэкфилл\w*|backfill", re.IGNORECASE)

# The two unmeasured figures, in the spellings a plan would use: both decimal
# separators, and the ru/en unit words, because the docs are written in both.
# Each pairs with the context its paragraph must also carry to count, or None.
_HEARSAY_FIGURES = (
    (re.compile(r"1[.,]22\s*(?:млн|миллион|million|M\b)", re.IGNORECASE), None),
    (re.compile(r"6[.,]5\s*(?:мин|min)", re.IGNORECASE), _TRANSFER_WINDOW),
)

# Any claim of the shape "<N> million rows" — the mistake is the shape, not the
# particular number. A scale word is required, so a plain observed count
# ("137 строк") is not a volume claim; the digit-group alternative catches the
# same magnitude written out ("1 220 000 строк").
_ROW_VOLUME_CLAIM = re.compile(
    r"""(?: \d+(?:[.,]\d+)? \s* (?: млн | миллион\w* | млрд | million | billion | M\b )
        |   \d{1,3} (?: [\s ,] \d{3} ){2,}
        )
        [^\n]{0,40}?
        (?: стро[кчн] | rows? )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_ESTIMATE_MARKERS = ("экстраполяц", "extrapolat")


def _paragraphs(text: str) -> list[str]:
    return [block for block in re.split(r"\n\s*\n", text) if block.strip()]


def _unlabelled(
    text: str,
    pattern: re.Pattern[str],
    context: re.Pattern[str] | None = None,
) -> list[str]:
    """Paragraphs matching ``pattern`` that do not own up to being estimates."""

    offenders = []
    for block in _paragraphs(text):
        if not pattern.search(block):
            continue
        if context is not None and not context.search(block):
            continue
        if any(marker in block.lower() for marker in _ESTIMATE_MARKERS):
            continue
        offenders.append(" ".join(block.split())[:160])
    return offenders


def _markdown_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every Markdown file the repo actually authors.

    Delegates to git's own notion of "files this repo owns" —
    ``git ls-files --cached --others --exclude-standard``, the same primitive
    ``tests/http_auth/negative_control.py`` already uses to materialise a
    mutation tree — instead of a hand-maintained directory exclusion list. A
    name-based list has to be kept in sync with `.gitignore` by hand and still
    rules on files it has no business seeing: a local `generated/`/`reports/`
    artifact, or (under the isolated-worktree pipeline) another branch's
    `.worktrees/` checkout of this very map.
    """

    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(root / rel for rel in out.stdout.split())


def test_the_hearsay_figures_are_never_restated_as_measurements():
    offenders: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for pattern, context in _HEARSAY_FIGURES:
            offenders += [
                f"{path.relative_to(REPO_ROOT)}: {block}"
                for block in _unlabelled(text, pattern, context)
            ]

    assert not offenders, (
        "the 1.22-million-rows / 6.5-minute figures were extrapolated from one "
        "table of a synthetic fixture, never measured against the live "
        "database; stating them without the word «экстраполяция» presents "
        "hearsay as a measurement: " + "; ".join(offenders)
    )


def test_migration_docs_label_every_volume_claim_as_an_estimate():
    offenders: list[str] = []
    for path in MIGRATION_DOCS:
        offenders += [
            f"{path.relative_to(REPO_ROOT)}: {block}"
            for block in _unlabelled(
                path.read_text(encoding="utf-8"), _ROW_VOLUME_CLAIM
            )
        ]

    assert not offenders, (
        "no row-count volume of the source has ever been measured, so a volume "
        "figure in the migration documents is an estimate and must say so: "
        + "; ".join(offenders)
    )


def test_schema_map_records_that_the_volume_was_never_measured():
    text = (DOCS_DIR / "srv01b-schema-map.md").read_text(encoding="utf-8")
    assert "## Объёмы" in text, (
        "docs/srv01b-schema-map.md must keep its volumes section: SRV-07/SRV-09 "
        "read this map for the source volume, and this is where the "
        "'extrapolated, never measured' correction is recorded"
    )
    for claim in ("экстраполяц", "синтетической фикстуры", "137 строк", "count(*)"):
        assert claim in text, (
            f"the volumes section lost a load-bearing claim: {claim!r}"
        )


def test_the_repo_wide_scan_covers_authored_docs_and_skips_vendored_ones():
    """Under-exclusion is one quiet failure: a scan that misses this repo's
    own docs would never catch anything."""

    scanned = _markdown_files()
    assert DOCS_DIR / "srv01b-schema-map.md" in scanned
    assert REPO_ROOT / "CHANGELOG.md" in scanned
    assert not [
        path
        for path in scanned
        if {"site-packages", "node_modules"} & set(path.parts)
        or any(part.startswith((".venv", "venv")) for part in path.parts)
    ]


def test_the_scan_skips_gitignored_docs_not_just_vendored_dependency_trees(tmp_path):
    """Over-exclusion is the quiet failure this gate actually had: a
    gitignored, machine-written doc — a `generated/` report, or another
    branch's `.worktrees/` copy of this very map — is neither a dependency
    tree nor named like one, so a vendored-name check never catches it. It
    must still be out of scope: this gate has no business ruling on somebody
    else's row counts."""

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("generated/\n.worktrees/\n", encoding="utf-8")

    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    (generated_dir / "report.md").write_text(
        "Ожидаем 2 миллиона строк.\n", encoding="utf-8"
    )

    stale_worktree_copy = tmp_path / ".worktrees" / "other-branch" / "docs"
    stale_worktree_copy.mkdir(parents=True)
    (stale_worktree_copy / "srv01b-schema-map.md").write_text(
        "stale copy from another branch\n", encoding="utf-8"
    )

    (tmp_path / "README.md").write_text("authored\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=tmp_path, check=True)

    assert _markdown_files(root=tmp_path) == [tmp_path / "README.md"]


# --- negative controls: the gate has to be able to fail -------------------


def test_gate_flags_an_unlabelled_hearsay_figure():
    """The exact regression: the number pasted into a plan as a fact."""

    plan = "Объём переноса — ≈1.22 млн строк, окно бэкфилла ≈6.5 минут."
    assert _unlabelled(plan, *_HEARSAY_FIGURES[0])
    assert _unlabelled(plan, *_HEARSAY_FIGURES[1])
    assert _unlabelled(plan, _ROW_VOLUME_CLAIM)


def test_gate_flags_a_restatement_that_changes_the_number():
    """A literal-only gate would miss these; the same mistake is being made."""

    for restatement in (
        "Источник несёт около 1.4 млн строк.",
        "The source holds roughly 1 220 000 rows.",
        "Ожидаем 2 миллиона строк в run_event.",
    ):
        assert _unlabelled(restatement, _ROW_VOLUME_CLAIM), restatement


def test_gate_accepts_a_figure_that_owns_up_to_being_an_estimate():
    labelled = (
        "Оценка «≈1.22 млн строк» и выведенное из неё окно бэкфилла "
        "«≈6.5 минут» получены экстраполяцией с одной таблицы синтетической "
        "фикстуры."
    )
    assert not _unlabelled(labelled, *_HEARSAY_FIGURES[0])
    assert not _unlabelled(labelled, *_HEARSAY_FIGURES[1])
    assert not _unlabelled(labelled, _ROW_VOLUME_CLAIM)


def test_gate_does_not_mistake_an_observed_count_for_a_volume_claim():
    """`137 строк` is something somebody counted, not a projection."""

    assert not _unlabelled("В файле 137 строк, все таблицы пусты.", _ROW_VOLUME_CLAIM)
    assert not _unlabelled("Файл занимает ≈35 МБ на диске.", _ROW_VOLUME_CLAIM)


def test_gate_leaves_an_unrelated_duration_alone():
    """A real 6.5-minute measurement of something else is not this figure."""

    assert not _unlabelled("Прогон CI занимает 6.5 мин.", *_HEARSAY_FIGURES[1])


def test_gate_does_not_mistake_every_window_or_transfer_for_the_backfill_estimate():
    """"окно"/"перенос" are ordinary words elsewhere in this repo — the SRV-05
    payload's visibility window, a queue message transfer. A genuine
    measurement of one of those must not be forced to call itself an
    extrapolation just because it shares that vocabulary with the backfill
    estimate this gate exists to catch."""

    for sentence in (
        "Окно видимости очереди прогнали замером и получили 6.5 мин.",
        "Перенос сообщения между очередями занимает 6.5 мин.",
    ):
        assert not _unlabelled(sentence, *_HEARSAY_FIGURES[1]), sentence


def test_reformatting_a_labelled_list_with_blank_lines_does_not_flip_the_verdict():
    """A tight numbered list and the same list with blank lines between its
    items render as near-identical Markdown; the verdict must not depend on
    which one was used. That only holds if every item that carries a figure
    also carries its own label, rather than borrowing one from a neighbour it
    merely happens to share a blank-line block with."""

    tight = (
        "1. Оценка «≈1.22 млн строк» — экстраполяция с фикстуры.\n"
        "2. Окно бэкфилла в «≈6.5 минут» — та же экстраполяция, другая цифра."
    )
    loose = tight.replace("\n", "\n\n")

    for variant in (tight, loose):
        assert not _unlabelled(variant, *_HEARSAY_FIGURES[0]), variant
        assert not _unlabelled(variant, *_HEARSAY_FIGURES[1]), variant
