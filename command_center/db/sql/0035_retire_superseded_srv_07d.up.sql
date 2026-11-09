-- VOYN-W0-AICC-SRV-07d-STALL-df89d893ef053818: the original task was
-- parked after VOYN_LEASE_REFUSED and intentionally retried under
-- VOYN-W0-AICC-SRV-07d-RETRY.  The retry shipped in PR #446 at
-- 0bc752688fe8baf49c44970f666c7dd1c65c2fac.  The original row was later
-- left READY_TO_REVIEW without PR evidence, so pr_evidence can only report
-- no_open_pr_on_task_branch forever: no PR was (or should be) opened for
-- that superseded branch.
--
-- Retire only that exact, still-stale shape.  Do not invent PR evidence for
-- the original task and do not weaken reconcile_pr_evidence's exact-branch
-- rule.  A row that has changed since the incident is deliberately untouched.
DO $$
DECLARE
    v_revision bigint;
BEGIN
    UPDATE backlog_task t
       SET status = 'REJECTED',
           revision = t.revision + 1,
           updated_at = now(),
           body = coalesce(t.body, '') || E'\n\nSuperseded by '
               'VOYN-W0-AICC-SRV-07d-RETRY; delivered by PR #446 at '
               '0bc752688fe8baf49c44970f666c7dd1c65c2fac.'
     WHERE t.task_id = 'VOYN-W0-AICC-SRV-07d'
       AND t.kind = 'task'
       AND t.status = 'READY_TO_REVIEW'
       AND NOT EXISTS (
           SELECT 1 FROM backlog_evidence e
            WHERE e.task_id = t.task_id AND e.kind = 'pr'
       )
    RETURNING t.revision INTO v_revision;

    IF v_revision IS NOT NULL THEN
        PERFORM _backlog_audit(
            'VOYN-W0-AICC-SRV-07d',
            'retire_superseded_task',
            'granted',
            'delivered_by_retry',
            jsonb_build_object(
                'replacement_task_id', 'VOYN-W0-AICC-SRV-07d-RETRY',
                'pull_request', 'https://github.com/dimastov-lab/ai-command-center/pull/446',
                'merge_commit', '0bc752688fe8baf49c44970f666c7dd1c65c2fac',
                'revision', v_revision
            )
        );
    END IF;
END
$$;
