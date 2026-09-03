-- VOYN-W0-AICC-DEFER-QUEUE-ROOT-CAUSE-SWEEP: one classification, not three.
--
-- backlog_resume_deferred (0014) already decides, per task, whether a
-- DEFER_TO_USER park is a technical cascade exhaustion (safe to retry
-- automatically) or an owner decision (must stay parked for a human) -- but
-- that decision lived only inside the SECURITY DEFINER function's
-- procedural IF chain, answerable only by attempting a resume. The
-- planner's own tick (0014's plan_once integration) re-derived the SAME
-- four conditions as a hand-written WHERE clause -- its own comment calls
-- it a mirror "purely as a FILTER" -- which means one classification had
-- two independent SQL expressions of it: a maintenance hazard the moment
-- either changes without the other, and a rule nobody could answer as a
-- plain count without re-deriving the gate a third time.
--
-- backlog_defer_classification makes the classification itself a queryable,
-- read-only fact, in exactly one place: for every task currently parked in
-- DEFER_TO_USER, the bucket -- 'infra_induced_safe_to_retry' (would be
-- GRANTED by backlog_resume_deferred right now) or 'genuine_owner_decision'
-- (would be REFUSED, with the precise refusal reason 0014 uses) -- mirroring
-- backlog_resume_deferred's own gate order exactly: no machine park
-- evidence, then a non-technical (owner-authored) park reason, then a
-- superseded park, then an exhausted resume budget. The planner tick is
-- refactored to select its resume candidates FROM this view instead of
-- carrying its own copy of the predicate.

CREATE VIEW backlog_defer_classification AS
    SELECT t.task_id, t.wave, t.priority, t.title, t.repo,
           park.reason AS park_reason,
           coalesce(resumes.granted, 0) AS resumes_granted,
           CASE
               WHEN park.event_id IS NULL THEN 'genuine_owner_decision'
               WHEN park.reason IS NULL
                    OR park.reason NOT LIKE 'cascade_exhausted:%'
                   THEN 'genuine_owner_decision'
               WHEN superseded.found THEN 'genuine_owner_decision'
               WHEN coalesce(resumes.granted, 0) >= 3 THEN 'genuine_owner_decision'
               ELSE 'infra_induced_safe_to_retry'
           END AS bucket,
           CASE
               WHEN park.event_id IS NULL THEN 'no_machine_park_evidence'
               WHEN park.reason IS NULL
                    OR park.reason NOT LIKE 'cascade_exhausted:%'
                   THEN 'owner_decision_park'
               WHEN superseded.found THEN 'superseded_park_evidence'
               WHEN coalesce(resumes.granted, 0) >= 3 THEN 'resume_budget_exhausted'
               ELSE 'cascade_exhausted_technical'
           END AS classification_reason
      FROM backlog_task t
      LEFT JOIN LATERAL (
          SELECT e.reason, e.event_id FROM backlog_event e
           WHERE e.task_id = t.task_id
             AND e.event = 'return_to_pool'
             AND e.outcome = 'granted'
             AND e.detail->>'target' = 'DEFER_TO_USER'
           ORDER BY e.event_id DESC LIMIT 1
      ) park ON true
      LEFT JOIN LATERAL (
          SELECT count(*) AS granted FROM backlog_event e
           WHERE e.task_id = t.task_id
             AND e.event = 'resume_deferred'
             AND e.outcome = 'granted'
      ) resumes ON true
      LEFT JOIN LATERAL (
          SELECT EXISTS (
              SELECT 1 FROM backlog_event e2
               WHERE e2.task_id = t.task_id
                 AND e2.outcome = 'granted'
                 AND e2.event IN ('upsert', 'transition', 'triage',
                                  'dispatch', 'return_to_pool', 'resume_deferred')
                 AND e2.event_id > park.event_id
          ) AS found
      ) superseded ON true
     WHERE t.status = 'DEFER_TO_USER' AND t.kind = 'task';
