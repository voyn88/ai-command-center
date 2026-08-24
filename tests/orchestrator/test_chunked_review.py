from __future__ import annotations

from command_center.orchestrator import planner, review_merge

TASK = "VOYN-W0-CHUNKED"
PR = "https://github.com/voyn88/ai-command-center/pull/380"
HEAD = "e" * 40


def _rows_for(chunks, verdicts=None, *, result_head=HEAD):
    verdicts = verdicts or ["ACCEPT"] * len(chunks)
    rows = []
    for chunk, verdict in zip(chunks, verdicts, strict=True):
        key = review_merge._chunk_review_key(TASK, PR, HEAD, chunk)
        payload = {
            "review_chunk": {
                "version": 1,
                "index": chunk.index,
                "count": chunk.count,
                "content_hash": chunk.content_hash,
                "manifest_hash": chunk.manifest_hash,
                "head_sha": HEAD,
            }
        }
        result = {"result_text": f"VERDICT: {verdict}\nHEAD_SHA: {result_head}"}
        rows.append((key, "succeeded", payload, result))
    return rows


def _publish(monkeypatch, rows):
    def fake_rows(_factory, sql, _params=()):
        if "SELECT t.task_id" in sql:
            return [(TASK, PR)]
        if "SELECT i.idempotency_key" in sql:
            return rows
        raise AssertionError(sql)

    posted = []
    remediated = []
    monkeypatch.setattr(review_merge, "_rows", fake_rows)
    monkeypatch.setattr(
        review_merge, "_has_accept_marker", lambda _repo, _pr: (False, HEAD)
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", object)
    monkeypatch.setattr(
        review_merge,
        "_post_marker_as_bot",
        lambda _creds, pr, verdict, sha: (posted.append((pr, verdict, sha)) or True, ""),
    )
    monkeypatch.setattr(
        review_merge,
        "_remediate_rejection",
        lambda _factory, task, _pr, _head, text: (
            remediated.append((task, text)) or f"{task}-REM"
        ),
    )
    report = review_merge.publish_review_verdicts(None, "/repo")
    return report, posted, remediated


def test_missing_chunk_cannot_publish_an_acceptance_marker(monkeypatch):
    chunks = review_merge._diff_chunks("diff --git a/a b/a\n" + "x\n" * 120, cap=80)
    rows = _rows_for(chunks)
    report, posted, _remediated = _publish(monkeypatch, rows[:-1])

    assert posted == []
    assert "review_chunks_missing" in report.skipped[0][1]


def test_chunk_verdict_for_a_stale_sha_cannot_publish_marker(monkeypatch):
    chunks = review_merge._diff_chunks("diff --git a/a b/a\n" + "x\n" * 120, cap=80)
    rows = _rows_for(chunks, result_head="d" * 40)
    report, posted, _remediated = _publish(monkeypatch, rows)

    assert posted == []
    assert report.skipped == [(TASK, "review_chunk_head_sha_mismatch:0")]


def test_failed_chunk_waits_without_publishing_a_marker(monkeypatch):
    chunks = review_merge._diff_chunks("diff --git a/a b/a\n" + "x\n" * 120, cap=80)
    rows = _rows_for(chunks)
    key, _state, payload, _result = rows[1]
    rows[1] = (key, "failed", payload, None)
    report, posted, _remediated = _publish(monkeypatch, rows)

    assert posted == []
    assert report.skipped == [(TASK, "review_chunk_not_succeeded:1:failed")]


def test_one_rejected_chunk_dispatches_remediation_and_never_posts_marker(monkeypatch):
    chunks = review_merge._diff_chunks("diff --git a/a b/a\n" + "x\n" * 120, cap=80)
    verdicts = ["ACCEPT"] * len(chunks)
    verdicts[1] = "REJECT"
    rows = _rows_for(chunks, verdicts)
    key, _state, payload, _result = rows[0]
    rows[0] = (key, "running", payload, None)
    report, posted, remediated = _publish(
        monkeypatch, rows
    )

    assert posted == []
    assert report.remediated == [(TASK, f"{TASK}-REM")]
    assert remediated and "Chunk 2/" in remediated[0][1]


def test_chunk_order_and_hashes_are_deterministic_and_cover_the_whole_diff():
    file_a = "diff --git a/a b/a\n@@ -1 +1 @@\n-old\n+new\n"
    file_b = "diff --git a/b b/b\n@@ -1 +1 @@\n-x\n+y\n"
    first = review_merge._diff_chunks(file_a + file_b, cap=45)
    repeat = review_merge._diff_chunks(file_a + file_b, cap=45)
    reordered = review_merge._diff_chunks(file_b + file_a, cap=45)

    assert first == repeat
    assert "".join(chunk.text for chunk in first) == file_a + file_b
    assert all(len(chunk.text) <= 45 for chunk in first)
    assert first[0].manifest_hash != reordered[0].manifest_hash
    assert [review_merge._chunk_review_key(TASK, PR, HEAD, chunk) for chunk in first] != [
        review_merge._chunk_review_key(TASK, PR, HEAD, chunk)
        for chunk in reordered
    ]
    outcome, _detail = review_merge._aggregate_chunk_verdict(
        list(reversed(_rows_for(first))),
        HEAD,
        review_merge._chunk_key_prefix(TASK, PR, HEAD) or "",
    )
    assert outcome == "ACCEPT"

    tampered = _rows_for(first)
    key, state, payload, result = tampered[0]
    payload["review_chunk"]["content_hash"] = "0" * 64
    key = f"{review_merge._chunk_key_prefix(TASK, PR, HEAD)}0000:{'0' * 64}"
    tampered[0] = (key, state, payload, result)
    outcome, detail = review_merge._aggregate_chunk_verdict(
        tampered,
        HEAD,
        review_merge._chunk_key_prefix(TASK, PR, HEAD) or "",
    )
    assert (outcome, detail) == ("WAIT", "review_chunk_manifest_hash_mismatch")


def test_large_diff_enqueues_one_exact_manifest_item_per_bounded_chunk(monkeypatch):
    diff = "diff --git a/large b/large\n@@ -1 +1 @@\n" + "-old\n+new\n" * 13_000
    monkeypatch.setattr(review_merge, "cascade_for", lambda _kind: ["copilot"])
    monkeypatch.setattr(
        review_merge,
        "_rows",
        lambda _factory, _sql, _params=(): [(TASK, PR)],
    )
    monkeypatch.setattr(planner, "repo_route", lambda _repo: ("AICC", "/repo"))
    monkeypatch.setattr(
        review_merge, "_pr_diff_and_head", lambda _repo, _pr: (diff, HEAD)
    )
    calls = []

    report = review_merge.review_once(
        None,
        lambda queue, key, payload, task, attempts: calls.append(
            (queue, key, payload, task, attempts)
        ),
        "/repo",
    )

    chunks = review_merge._diff_chunks(diff)
    assert report.reviewed == [(TASK, PR)]
    assert len(calls) == len(chunks) > 1
    assert [call[1] for call in calls] == [
        review_merge._chunk_review_key(TASK, PR, HEAD, chunk) for chunk in chunks
    ]
    assert {call[2]["review_chunk"]["manifest_hash"] for call in calls} == {
        chunks[0].manifest_hash
    }
