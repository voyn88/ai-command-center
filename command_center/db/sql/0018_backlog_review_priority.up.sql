-- 0018: the invalidated-verdict priority queue (VOYN-W0-AICC-INVALIDATED-
-- VERDICT-PRIORITY-REREVIEW).
--
-- merge_once's BEHIND branch (`_gh(["pr", "update-branch", pr_url], ...)`)
-- moves a PR's head forward on a PR that was, by construction, ONLY reached
-- there because it already carried an ACCEPT marker on its PRE-update head
-- (`_pr_is_mergeable` is the readiness gate one line above). The marker is
-- head-keyed, so moving the head off it is the merge tick itself
-- invalidating its own accepted verdict. When the diff digest ALSO changed
-- (a real re-review is required -- see VOYN-W0-AICC-MARKER-CARRYOVER-ON-
-- BRANCH-UPDATE-REM, which carries the marker forward instead of dropping it
-- when the digest is unchanged), that fresh review previously had no
-- distinguished path back into the queue: it waited for `review_once`'s
-- rotating `_scan_tasks` keyset window to reach that task_id again, which
-- can be a full lap of the id space behind whatever `max_per_tick` lets
-- through per tick -- observed live 2026-09-07 on PR #766, 20+ minutes
-- with ~10 other PRs still queued ahead of it in scan order.
--
-- This table is that distinguished path: a one-shot priority marker, set by
-- `merge_once` the instant it performs such an update, and drained by
-- `review_once` BEFORE it touches the rotating scan window -- so the very
-- next review tick enqueues the new-head review first. Draining is bounded
-- by the same `max_per_tick` the rotating scan already respects, so a burst
-- of priority markers can only ever shrink that tick's rotation budget, not
-- eliminate it: the rotation always gets to run with whatever budget the
-- priority drain left behind, never starved outright.
--
-- Idempotent by PRIMARY KEY (task_id, pr_url): a second mark before the
-- first is drained is a no-op refresh of `queued_at`, not a duplicate row --
-- so a task cannot inflate its own share of a tick's drain budget by being
-- marked repeatedly. Writes travel through `backlog_review_priority_mark`
-- (idempotent, audited) and draining through `backlog_review_priority_pop`
-- (a bounded, ordered, atomic delete-and-return) -- the same idiom as every
-- other backlog control table (0005): no role gets raw table DML.
CREATE TABLE backlog_review_priority (
    task_id    text        NOT NULL REFERENCES backlog_task(task_id),
    pr_url     text        NOT NULL,
    queued_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backlog_review_priority_pr_url_present CHECK (length(pr_url) > 0),
    PRIMARY KEY (task_id, pr_url)
);

CREATE FUNCTION backlog_review_priority_mark(p_task_id text, p_pr_url text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'review_priority', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF p_pr_url IS NULL OR length(p_pr_url) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'review_priority', 'rejected', 'empty_value');
        v.reason := 'empty_value'; v.revision := t.revision;
        RETURN v;
    END IF;
    INSERT INTO backlog_review_priority (task_id, pr_url)
    VALUES (p_task_id, p_pr_url)
    ON CONFLICT (task_id, pr_url) DO UPDATE SET queued_at = now();
    PERFORM _backlog_audit(p_task_id, 'review_priority', 'granted', 'marked',
                           jsonb_build_object('pr', p_pr_url));
    v.ok := true; v.reason := 'marked'; v.revision := t.revision;
    RETURN v;
END
$$;

-- Bounded, ordered, atomic pop: the oldest `p_limit` markers are deleted and
-- returned together, so a concurrent same-tick caller can never observe (let
-- alone re-drain) a row this call already claimed.
CREATE FUNCTION backlog_review_priority_pop(p_limit integer)
    RETURNS TABLE (task_id text, pr_url text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    RETURN QUERY
    DELETE FROM backlog_review_priority p
     WHERE p.ctid IN (
        SELECT r.ctid FROM backlog_review_priority r
         ORDER BY r.queued_at
         LIMIT greatest(p_limit, 0)
     )
    RETURNING p.task_id, p.pr_url;
END
$$;

REVOKE ALL ON FUNCTION backlog_review_priority_mark(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION backlog_review_priority_pop(integer) FROM PUBLIC;
REVOKE ALL ON backlog_review_priority FROM PUBLIC;
