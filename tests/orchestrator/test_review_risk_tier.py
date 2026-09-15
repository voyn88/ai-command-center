"""Risk-tiered independent review (VOYN-W0-AICC-REVIEW-RISK-TIER-REM).

Two things are pinned here, matching the two rejections of PR #707:

1. The risk class is deterministic, and the LOW tier -- the only class that
   changes any behaviour -- is unreachable for a test-tree diff, for agent
   control text that happens to be markdown, and for anything the strict
   diff parser cannot account for byte-for-byte.
2. The latency measurement's "succeeded" gate is a gate the code actually
   applies, not one only its docstring believes in.
"""

from __future__ import annotations

import importlib.util
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from command_center.orchestrator import planner, review_merge
from command_center.orchestrator.review_merge import (
    VerdictRow,
    median_and_p95,
    percentile,
    review_cycle_and_chunk,
    review_verdict_latencies,
    summarize_verdict_latencies,
)

TASK = "VOYN-W0-RISK"
PR = "https://github.com/voyn88/ai-command-center/pull/707"
BASE, HEAD = "c" * 40, "e" * 40

ROOT = Path(__file__).resolve().parents[2]


def _load_latency_script():
    spec = importlib.util.spec_from_file_location(
        "review_verdict_latency_script",
        ROOT / "scripts" / "review_verdict_latency.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file(path, *, body="-old\n+new\n", old=1, new=1, extended=("index 1..2 100644",)):
    """One `diff --git` section for `path`, in the shape git emits."""
    header = "\n".join(extended)
    header = header + "\n" if header else ""
    return (
        f"diff --git a/{path} b/{path}\n"
        + header
        + f"--- a/{path}\n+++ b/{path}\n"
        + f"@@ -1,{old} +1,{new} @@\n"
        + body
    )


def _snapshot(text):
    return review_merge._PRSnapshot.create(text, BASE, HEAD)


# -- 1. the class itself ----------------------------------------------------


def test_documentation_only_diff_is_low_and_classification_is_deterministic():
    diff = (
        _file("README.md")
        + _file("docs/guide.rst")
        + _file("docs/desktop/ARCHITECTURE.md")
        + _file("CHANGELOG.md")
        + _file("LICENSE")
    )

    assert review_merge._review_pr_risk(diff) == review_merge._RISK_LOW
    # Pure: same bytes, same class, no clock or filesystem involved.
    assert {review_merge._review_pr_risk(diff) for _ in range(5)} == {
        review_merge._RISK_LOW
    }


def test_a_test_only_diff_is_never_low_risk():
    """THE rejection of PR #707 chunk 0/3.

    Deleting an assertion touches nothing but `tests/`. If that bought a
    diff the reduced-redundancy budget, the cheapest way to defeat review
    would be to change only the files that prove the system works."""
    deleted_assertion = _file(
        "tests/orchestrator/test_merge_gate.py",
        body="-    assert marker_is_independent(pr)\n reviewed = True\n",
        old=2,
        new=1,
    )

    assert review_merge._review_pr_risk(deleted_assertion) != review_merge._RISK_LOW
    assert review_merge._review_prompt_budget_bytes(
        review_merge._review_pr_risk(deleted_assertion)
    ) == review_merge._MAX_REVIEW_PROMPT_BYTES


@pytest.mark.parametrize(
    "path",
    [
        "tests/data/expected_report.md",       # a prose-looking test fixture
        "test/legacy/notes.txt",
        "command_center/orchestrator/testing/fixtures.md",
        "docs/test_walkthrough.md",            # basename says test
        "docs/rendering_test.md",
    ],
)
def test_prose_under_a_test_root_is_not_low_risk(path):
    assert review_merge._review_pr_risk(_file(path)) != review_merge._RISK_LOW


@pytest.mark.parametrize(
    "path",
    [
        # Markdown that code in THIS repository reads, and so is not prose:
        "projects/AICC.md",                   # project_config.py agent context
        "context/AIOS_CONTEXT.md",            # project_config.py agent context
        "roadmap/w0/lane.md",                 # portfolio_models.py lane parser
        "docs/adr/0011-governed-autonomy.md",  # asserted on by a fitness test
        # Root markdown this module does not name. Might be read by something;
        # the fast path is not the place to find out.
        "ARCHITECTURE.md",
        "INBOX.md",
        "ROADMAP_STATE.md",
        "DECISIONS.md",
    ],
)
def test_machine_read_markdown_is_not_treated_as_prose(path):
    """Extension is not evidence of prose. An allow-list of locations is,
    and it fails safe when the next machine-read `.md` is added."""
    assert review_merge._review_pr_risk(_file(path)) != review_merge._RISK_LOW


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-ci-linux.lock",
        "uv.lock",
        "aios-sdk.lock.json",
    ],
)
def test_a_dependency_manifest_is_not_a_text_document(path):
    """`requirements.txt` is a `.txt` and is also the supply chain."""
    assert review_merge._review_pr_risk(_file(path)) == review_merge._RISK_HIGH


@pytest.mark.parametrize(
    "path",
    [
        "prompts/review.md",                   # the reviewer's own prompt
        "prompts/final_gate.md",
        ".claude/settings.json",
        "CLAUDE.md",                           # agent instruction file
        "AGENTS.md",
        ".github/copilot-instructions.md",
        ".github/workflows/ci.yml",
        ".github/ISSUE_TEMPLATE/bug.md",
        "CODEOWNERS",
        "command_center/db/sql/0011_x.up.sql",
        "docs/authentication.md",
        "docs/release_process.md",
        "docs/security_model.md",
        "docs/schema_notes.md",
        "docs/migration_plan.md",
        "deploy/control.service",
        "scripts/ci/gate.py",
    ],
)
def test_control_text_and_deny_listed_paths_are_high_risk(path):
    """Markdown is not automatically prose. `prompts/*.md` IS the review
    policy the agents execute; a weakened one is a weakened gate."""
    assert review_merge._review_pr_risk(_file(path)) == review_merge._RISK_HIGH


@pytest.mark.parametrize(
    "path", ["docs/design.md", "docs/blocking.md", "docs/monkeypatching.md"]
)
def test_the_keyword_deny_list_over_matches_and_that_is_the_documented_trade(path):
    """`sign` catches `design`, `lock` catches `blocking`, `key` catches
    `monkeypatch`. Pinned so the cost of the substring match stays visible
    rather than being rediscovered as a surprise."""
    assert review_merge._review_pr_risk(_file(path)) == review_merge._RISK_HIGH


def test_a_top_level_testing_directory_is_a_test_root_too():
    assert review_merge._review_pr_risk(
        _file("testing/notes.md")
    ) != review_merge._RISK_LOW


def test_any_non_documentation_path_drops_the_whole_diff_off_the_fast_path():
    mixed = _file("README.md") + _file("app.py")
    assert review_merge._review_pr_risk(mixed) == review_merge._RISK_STANDARD


def test_a_rename_out_of_a_code_tree_cannot_hide_behind_its_destination():
    rename = (
        "diff --git a/command_center/orchestrator/core.py b/docs/core.md\n"
        "similarity index 94%\n"
        "rename from command_center/orchestrator/core.py\n"
        "rename to docs/core.md\n"
        "index 1..2 100644\n"
        "--- a/command_center/orchestrator/core.py\n"
        "+++ b/docs/core.md\n"
        "@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    assert review_merge._review_pr_risk(rename) != review_merge._RISK_LOW


@pytest.mark.parametrize(
    "extended",
    [
        ("new file mode 120000", "index 0..2"),      # doc becomes a symlink
        ("old mode 100644", "new mode 100755"),      # doc gains the exec bit
        ("index 1..2 160000",),                      # submodule pointer
    ],
)
def test_a_documentation_path_with_a_non_regular_mode_is_not_low_risk(extended):
    assert review_merge._review_pr_risk(
        _file("docs/guide.md", extended=extended)
    ) != review_merge._RISK_LOW


@pytest.mark.parametrize(
    "diff",
    [
        "",
        "not a diff at all\n",
        "diff --git a/README.md b/README.md\nGIT binary patch\nliteral 4\n",
        "diff --git a/docs/x.png b/docs/x.png\n"
        "index 1..2 100644\nBinary files a/docs/x.png and b/docs/x.png differ\n",
        # A hunk that claims more lines than it carries: truncated or forged.
        "diff --git a/README.md b/README.md\nindex 1..2 100644\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1,9 +1,9 @@\n-a\n+b\n",
        # An unquoted path this parser will not guess at.
        'diff --git "a/d\\303\\251cor.md" "b/d\\303\\251cor.md"\n'
        "old mode 100644\nnew mode 100644\n",
    ],
)
def test_an_unparseable_diff_fails_safe_to_non_low(diff):
    assert review_merge._review_pr_risk(diff) != review_merge._RISK_LOW
    assert review_merge._diff_changed_paths(diff) is None


@pytest.mark.parametrize(
    ("label", "diff", "expected_low"),
    [
        # Shapes git really emits for a docs change, all of which must still
        # reach the fast path or the tier is worth nothing in practice.
        (
            "added empty file",
            "diff --git a/docs/new.md b/docs/new.md\n"
            "new file mode 100644\nindex 0000000..e69de29\n",
            True,
        ),
        (
            "deleted file",
            "diff --git a/docs/old.md b/docs/old.md\n"
            "deleted file mode 100644\nindex 1..0000000\n"
            "--- a/docs/old.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n",
            True,
        ),
        (
            "no newline at eof markers",
            "diff --git a/README.md b/README.md\nindex 1..2 100644\n"
            "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n"
            "-a\n\\ No newline at end of file\n"
            "+b\n\\ No newline at end of file\n",
            True,
        ),
        (
            "two hunks in one file",
            "diff --git a/docs/g.md b/docs/g.md\nindex 1..2 100644\n"
            "--- a/docs/g.md\n+++ b/docs/g.md\n"
            "@@ -1,2 +1,2 @@\n-a\n+b\n c\n@@ -10,1 +10,2 @@\n d\n+e\n",
            True,
        ),
        # Shapes the grammar does not model, which must fail safe.
        (
            "crlf line endings",
            "diff --git a/README.md b/README.md\r\nindex 1..2 100644\r\n"
            "--- a/README.md\r\n+++ b/README.md\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n",
            False,
        ),
        (
            "combined merge diff",
            "diff --cc README.md\nindex 1,2..3\n--- a/README.md\n"
            "+++ b/README.md\n@@@ -1,1 -1,1 +1,1 @@@\n- a\n +b\n",
            False,
        ),
        (
            "bare header with neither a path line nor a mode line",
            "diff --git a/docs/x.md b/docs/x.md\n",
            False,
        ),
        (
            "trailing bytes after the last hunk",
            "diff --git a/README.md b/README.md\nindex 1..2 100644\n"
            "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
            "unexpected\n",
            False,
        ),
    ],
)
def test_the_grammar_accepts_real_git_output_and_refuses_the_rest(
    label, diff, expected_low
):
    is_low = review_merge._review_pr_risk(diff) == review_merge._RISK_LOW
    assert is_low is expected_low, label


def test_diff_content_that_looks_like_a_file_header_is_read_as_content():
    """A removed markdown line beginning `-- a/x` renders as `--- a/x` at
    column 0, and an added line beginning `++ b/x` renders as `+++ b/x`. A
    line-wise scan would read those as file headers; the grammar walk knows
    it is inside a hunk whose line counts it is tracking."""
    forged = _file(
        "README.md",
        body=(
            " intro\n"
            "-- a/command_center/orchestrator/review_merge.py\n"
            "++ b/command_center/orchestrator/review_merge.py\n"
            "+real addition\n"
            " tail\n"
        ),
        old=3,
        new=4,
    )

    files = review_merge._diff_changed_paths(forged)

    assert files is not None
    assert {path for entry in files for path in entry.paths} == {"README.md"}
    assert review_merge._review_pr_risk(forged) == review_merge._RISK_LOW


def test_a_forged_header_cannot_conceal_a_real_code_file():
    """The other direction: content that mimics a header must not let a
    genuine second file section go unseen."""
    concealed = (
        _file(
            "README.md",
            body=" intro\n+++ b/README.md\n+@@ -1,1 +1,1 @@\n",
            old=1,
            new=3,
        )
        + _file("command_center/orchestrator/auth_gate.py")
    )

    files = review_merge._diff_changed_paths(concealed)

    assert files is not None
    assert "command_center/orchestrator/auth_gate.py" in {
        path for entry in files for path in entry.paths
    }
    assert review_merge._review_pr_risk(concealed) == review_merge._RISK_HIGH


# -- 2. what the class is allowed to change ---------------------------------


def _one_line_diff(path, filler):
    return _file(path, body="-old\n+" + filler + "\n")


def test_low_risk_collapses_the_fan_out_only_for_documentation():
    """A docs diff that would have needed several chunks needs one. The same
    number of bytes under a code path still fans out exactly as before."""
    filler = "b" * 30_000
    docs = _snapshot(_one_line_diff("docs/guide.md", filler))
    code = _snapshot(_one_line_diff("command_center/orchestrator/planner.py", filler))

    doc_chunks = review_merge._review_chunks(docs, TASK, PR)
    code_chunks = review_merge._review_chunks(code, TASK, PR)

    assert [chunk.risk for chunk in doc_chunks] == [review_merge._RISK_LOW]
    assert len(doc_chunks) == 1
    assert len(code_chunks) > 1
    assert {chunk.risk for chunk in code_chunks} == {review_merge._RISK_STANDARD}


def test_the_low_risk_budget_is_bounded_and_falls_back_to_chunking():
    """The fast path is a bound, not an escape hatch: a docs diff too big
    for it chunks like anything else, and every chunk still fits."""
    huge = _snapshot(_one_line_diff("docs/guide.md", "b" * 400_000))

    chunks = review_merge._review_chunks(huge, TASK, PR)

    assert len(chunks) > 1
    assert "".join(chunk.text for chunk in chunks) == huge.text
    assert all(
        review_merge._prompt_size_bytes(
            review_merge._render_review_prompt(TASK, PR, huge, chunk)
        ) <= review_merge._LOW_RISK_MAX_REVIEW_PROMPT_BYTES
        for chunk in chunks
    )


def test_every_byte_of_a_low_risk_diff_still_reaches_the_reviewer():
    snapshot = _snapshot(_one_line_diff("docs/guide.md", "b" * 30_000))

    chunk = review_merge._review_chunks(snapshot, TASK, PR)[0]
    prompt = review_merge._render_review_prompt(TASK, PR, snapshot, chunk)

    assert chunk.text == snapshot.text
    assert '"risk_class":"low"' in prompt
    assert '"scope":"complete_diff"' in prompt
    # The verdict contract is identical for every tier.
    assert "VERDICT: ACCEPT or VERDICT: REJECT" in prompt
    assert "HEAD_SHA:" in prompt


def test_only_the_low_class_widens_the_budget():
    standard = review_merge._MAX_REVIEW_PROMPT_BYTES
    assert review_merge._review_prompt_budget_bytes(review_merge._RISK_LOW) == (
        review_merge._LOW_RISK_MAX_REVIEW_PROMPT_BYTES
    )
    for risk in (review_merge._RISK_HIGH, review_merge._RISK_STANDARD, "", "future"):
        assert review_merge._review_prompt_budget_bytes(risk) == standard
    # A chunk built by hand -- a fixture, the sizing probe -- is never LOW.
    assert review_merge._make_diff_chunks(["a", "b"])[0].risk == (
        review_merge._RISK_STANDARD
    )


def test_review_once_enqueues_one_item_for_docs_and_a_manifest_for_code(monkeypatch):
    filler = "b" * 30_000

    def run(diff):
        snapshot = _snapshot(_one_line_diff(*diff, filler))
        monkeypatch.setattr(
            review_merge, "_model_only_review_cascade",
            lambda: [{"executor": "codex"}],
        )
        monkeypatch.setattr(planner, "repo_route", lambda _: ("AICC", "/repo"))
        monkeypatch.setattr(
            review_merge, "_rows",
            lambda _f, sql, _p=(): [(TASK, PR)] if "backlog_task" in sql else [],
        )
        monkeypatch.setattr(
            review_merge, "_pr_window_expensive_read_decision",
            lambda *_a, **_k: review_merge._PrWindowReadDecision(True, "ok", {}),
        )
        monkeypatch.setattr(
            review_merge, "_pr_diff_and_head_with_pull", lambda *_a: snapshot
        )
        dispatched = []
        report = review_merge.review_once(
            None,
            lambda *args: dispatched.append(args),
            "/repo",
            task_id=TASK,
        )
        assert report.skipped == []
        return snapshot, dispatched

    _docs_snapshot, docs = run(("docs/guide.md",))
    _code_snapshot, code = run(("command_center/orchestrator/planner.py",))

    assert len(docs) == 1
    assert "review_chunk" not in docs[0][2]
    assert docs[0][2]["review_risk"] == review_merge._RISK_LOW
    assert len(code) > 1
    assert all("review_chunk" in call[2] for call in code)
    assert {call[2]["review_risk"] for call in code} == {review_merge._RISK_STANDARD}


@pytest.mark.parametrize(
    "gate",
    [
        "_pr_is_mergeable",
        "_aggregate_chunk_verdict",
        "_parse_verdict",
        "_post_marker_as_bot",
        "_has_accept_marker",
        "_review_key",
        "_chunk_review_key",
        "_carry_over_marker_if_patch_id_stable",
    ],
)
def test_no_mandatory_gate_reads_the_risk_class(gate):
    """The tier may only ever spend fewer prompts on the same bytes. None of
    the functions that decide whether a head is accepted, whether a marker
    may be posted, or whether a PR may merge is allowed to see it -- so no
    risk class, present or future, can widen what merges."""
    body = inspect.getsource(getattr(review_merge, gate))
    assert "risk" not in body.lower()


def test_the_required_merge_checks_are_untouched_by_tiering():
    assert review_merge.ReviewConfig().required_checks == (
        review_merge._DEFAULT_REQUIRED_MERGE_CHECKS
    )
    assert review_merge._DEFAULT_REQUIRED_MERGE_CHECKS == (
        "Final merge gate",
        "Acceptance gate (independent verdict on exact SHA)",
    )


# -- 3. the before/after measurement ----------------------------------------


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
WINDOW = NOW - timedelta(days=7)
CYCLE = (
    "review:VOYN-W0-RISK:707:" + HEAD + ":v9:base:" + BASE + ":diff:" + "d" * 64
)


def _row(key, *, state="succeeded", enqueued=0, result=None):
    return VerdictRow(
        idempotency_key=key,
        state=state,
        enqueued_at=NOW + timedelta(seconds=enqueued),
        result_at=None if result is None else NOW + timedelta(seconds=result),
    )


def _chunk_key(index):
    return f"{CYCLE}:chunk:{index:04d}:{'a' * 64}"


def test_percentile_and_median_match_linear_interpolation():
    assert percentile([10.0], 0.95) == 10.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)
    assert median_and_p95([]) is None
    assert median_and_p95([5.0, 1.0, 3.0]) == (3.0, pytest.approx(4.8))
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_a_cycle_latency_spans_first_enqueue_to_last_verdict():
    rows = [
        _row(_chunk_key(0), enqueued=0, result=100),
        _row(_chunk_key(1), enqueued=5, result=420),
        _row(_chunk_key(2), enqueued=9, result=200),
    ]

    latencies, in_flight, truncated, foreign = review_verdict_latencies(
        rows, window_start=WINDOW
    )

    assert latencies == [420.0]
    assert (in_flight, truncated, foreign) == (0, 0, 0)


def test_a_result_row_whose_item_did_not_succeed_does_not_land_a_chunk():
    """The rejection of PR #707 chunk 1/3: the docstring claimed a
    `work_item.state = 'succeeded'` gate that nothing applied. The gate is
    applied here, on the rows, so it holds whatever query produced them."""
    rows = [
        _row(_chunk_key(0), enqueued=0, result=100),
        _row(_chunk_key(1), state="dead", enqueued=0, result=50),
    ]

    latencies, in_flight, _truncated, _foreign = review_verdict_latencies(
        rows, window_start=WINDOW
    )

    assert latencies == []
    assert in_flight == 1
    # And the widened reading is opt-in and visibly different, so nobody can
    # believe the strict number while getting the loose one.
    widened, _, _, _ = review_verdict_latencies(
        rows, window_start=WINDOW, require_succeeded=False
    )
    assert widened == [100.0]


def test_an_unfinished_chunk_keeps_the_whole_cycle_out_of_the_distribution():
    rows = [
        _row(_chunk_key(0), enqueued=0, result=100),
        _row(_chunk_key(1), state="claimed", enqueued=0),
    ]

    latencies, in_flight, _t, _f = review_verdict_latencies(
        rows, window_start=WINDOW
    )

    assert latencies == [] and in_flight == 1


def test_retries_and_chunks_fold_into_one_cycle():
    assert review_cycle_and_chunk(_chunk_key(3)) == (CYCLE, "0003")
    assert review_cycle_and_chunk(_chunk_key(3) + ":retry:2") == (CYCLE, "0003")
    assert review_cycle_and_chunk(CYCLE) == (CYCLE, "single")
    assert review_cycle_and_chunk(CYCLE + ":retry:1") == (CYCLE, "single")
    assert review_cycle_and_chunk("verify:something") is None

    rows = [
        _row(_chunk_key(0), enqueued=0, result=30),
        _row(_chunk_key(0) + ":retry:1", enqueued=40, result=90),
    ]
    latencies, _i, _t, _f = review_verdict_latencies(rows, window_start=WINDOW)
    assert latencies == [90.0]


def test_a_cycle_that_may_predate_the_window_is_dropped_not_mismeasured():
    rows = [
        _row(_chunk_key(0), enqueued=-60, result=100),
        _row(_chunk_key(1), enqueued=0, result=120),
    ]

    latencies, _in_flight, truncated, _f = review_verdict_latencies(
        rows, window_start=NOW
    )

    assert latencies == [] and truncated == 1


def test_backdated_rows_and_foreign_keys_are_counted_never_measured():
    rows = [
        _row(CYCLE, enqueued=100, result=0),           # negative latency
        _row("verify:xyz", enqueued=0, result=10),     # not a review cycle
    ]

    summary = summarize_verdict_latencies(rows, window_start=WINDOW)

    assert summary.cycles == 0
    assert summary.truncated == 1
    assert summary.foreign_rows == 1
    assert summary.median_seconds is None
    assert "0 in-flight, 1 truncated, 1 foreign rows" in summary.render()


def test_summary_reports_counts_alongside_the_statistics():
    rows = [_row(f"{CYCLE}{index}", enqueued=0, result=index) for index in range(1, 5)]

    summary = summarize_verdict_latencies(rows, window_start=WINDOW)

    assert summary.cycles == 4
    assert summary.median_seconds == 2.5
    assert summary.p95_seconds == pytest.approx(3.85)
    assert "cycles     4" in summary.render()


# -- 4. the query the operator script actually runs -------------------------


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params):
        self.executed = (sql, params)

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj


def test_the_script_query_states_the_succeeded_gate_it_documents():
    script = _load_latency_script()

    sql = script._ROWS_SQL

    assert "wi.state = 'succeeded'" in sql
    # The item's OWN acknowledged result, not any result row that happens to
    # reference the item.
    assert "wr.result_id = wi.result_id" in sql
    assert "wr.work_item_id" not in sql
    assert "LIMIT" not in sql.upper()


def test_the_script_shapes_rows_for_the_pure_summariser():
    script = _load_latency_script()
    conn = _FakeConn([
        (_chunk_key(0), "succeeded", NOW, NOW + timedelta(seconds=12)),
        (_chunk_key(1), "claimed", NOW, None),
    ])

    rows = _fetched = script._fetch_rows(conn, WINDOW)

    assert conn.cursor_obj.executed[1] == (WINDOW,)
    assert [row.state for row in rows] == ["succeeded", "claimed"]
    assert rows[1].result_at is None
    assert summarize_verdict_latencies(_fetched, window_start=WINDOW).in_flight == 1


def test_the_script_prints_a_before_after_delta_end_to_end(monkeypatch, capsys):
    """The acceptance criterion is a measured before/after, so the command
    that produces it is exercised, not just its parts. v8 cycles took 600s,
    v9 cycles took 200s; the delta line has to say so."""
    script = _load_latency_script()
    v8 = CYCLE.replace(":v9:", ":v8:")

    def rows_for(cycle, seconds, count):
        return [
            (
                f"{cycle}{index}",
                "succeeded",
                NOW + timedelta(seconds=index),
                NOW + timedelta(seconds=index + seconds),
            )
            for index in range(count)
        ]

    conn = _FakeConn(rows_for(v8, 600, 4) + rows_for(CYCLE, 200, 4))

    class _Pool:
        opened = closed = False

        @staticmethod
        def open_pool(_config):
            _Pool.opened = True

        @staticmethod
        def close_pool():
            _Pool.closed = True

        @staticmethod
        def connection():
            from contextlib import nullcontext

            return nullcontext(conn)

    import command_center.db.pool as real_pool
    import command_center.db.config as real_config

    monkeypatch.setattr(real_pool, "open_pool", _Pool.open_pool)
    monkeypatch.setattr(real_pool, "close_pool", _Pool.close_pool)
    monkeypatch.setattr(real_pool, "connection", _Pool.connection)
    monkeypatch.setattr(real_config, "load_config", lambda: object())

    exit_code = script.main(["--days", "30", "--before", "v8", "--after", "v9"])
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert _Pool.opened and _Pool.closed
    assert "8 work items" in printed
    assert "delta: median -400.0s" in printed
    assert "p95 -400.0s" in printed
    assert "(v8 n=4 -> v9 n=4)" in printed


def test_the_script_refuses_to_invent_a_delta_it_cannot_measure(
    monkeypatch, capsys
):
    script = _load_latency_script()

    class _Pool:
        @staticmethod
        def open_pool(_config):
            return None

        @staticmethod
        def close_pool():
            return None

        @staticmethod
        def connection():
            from contextlib import nullcontext

            return nullcontext(_FakeConn([]))

    import command_center.db.pool as real_pool
    import command_center.db.config as real_config

    monkeypatch.setattr(real_pool, "open_pool", _Pool.open_pool)
    monkeypatch.setattr(real_pool, "close_pool", _Pool.close_pool)
    monkeypatch.setattr(real_pool, "connection", _Pool.connection)
    monkeypatch.setattr(real_config, "load_config", lambda: object())

    exit_code = script.main(["--before", "v8", "--after", "v9"])

    assert exit_code == 1
    assert "not computable" in capsys.readouterr().out


def test_the_script_buckets_by_review_policy_version():
    script = _load_latency_script()

    assert script._policy_version(CYCLE) == "v9"
    assert script._policy_version(CYCLE.replace(":v9:", ":v8:")) == "v8"
    assert script._policy_version("review:no-policy-here") == "unknown"
