-- AICC PostgreSQL foundation — initial schema (VOYN-W0-AICC-SRV-01a).
--
-- This is the PostgreSQL counterpart of the SQLite schema built by
-- `command_center/runtime/db` (SCHEMA_VERSION 23: the domain tables and 62
-- indexes). It is NOT a byte-for-byte transliteration: SQLite has no real
-- date, boolean or JSON type, so the source schema stores timestamps as ISO
-- TEXT, booleans as INTEGER 0/1 and documents as TEXT. Reproducing that here
-- would bake a known-weak foundation into the server database and force a
-- second migration later, so the PostgreSQL schema uses production types:
--
--   ISO-8601 TEXT timestamps  -> timestamptz
--   INTEGER 0/1 flags         -> boolean
--   *_json TEXT               -> jsonb
--   INTEGER PK AUTOINCREMENT  -> bigint GENERATED ALWAYS AS IDENTITY
--   REAL                      -> double precision
--
-- The consequence is carried by the data migration (VOYN-W0-AICC-SRV-07): its
-- importer must convert explicitly and its reconciliation report must compare
-- converted values rather than raw strings.
--
-- Two source columns intentionally stay `text`: `owner_item.due` and
-- `digest_item.day` are free-form user-entered day keys in the SQLite source,
-- not guaranteed-parseable dates, so narrowing them to `date` here would make
-- the import lossy for values the current product accepts.

CREATE TABLE task (
    id              text PRIMARY KEY,
    project         text        NOT NULL,
    title           text        NOT NULL,
    task_type       text        NOT NULL,
    legacy_task_id  text,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL
);

CREATE TABLE session (
    id              text PRIMARY KEY,
    task_id         text        NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    project         text        NOT NULL,
    repository_path text        NOT NULL,
    legacy_run_id   text,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL
);

CREATE TABLE run (
    id                     text PRIMARY KEY,
    session_id             text        NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    task_id                text        NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    sequence               integer     NOT NULL,
    is_resume              boolean     NOT NULL DEFAULT false,
    state                  text        NOT NULL,
    project                text        NOT NULL,
    task_type              text        NOT NULL,
    repository_path        text        NOT NULL,
    prompt                 text        NOT NULL,
    command_json           jsonb,
    timeout_seconds        integer,
    pid                    integer,
    process_start_identity text,
    pre_run_git_status     text,
    post_run_git_status    text,
    working_tree_changed   boolean,
    exit_code              integer,
    cancel_requested       boolean     NOT NULL DEFAULT false,
    cancel_requested_at    timestamptz,
    started_at             timestamptz,
    completed_at           timestamptz,
    version                integer     NOT NULL DEFAULT 0,
    created_at             timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,
    failure_reason         text,
    expected_branch        text,
    launch_source          text,
    commit_hash            text,
    pull_request_url       text,
    prompt_version         integer,
    first_output_at        timestamptz,
    provider_id            text        NOT NULL DEFAULT 'claude_code',
    provider_metadata_json jsonb,
    pre_run_head           text,
    capability_profile     text,
    capability_override    text,
    required_capabilities  text,
    granted_capabilities   text,
    capability_preflight   text,
    command_policy         text
);

CREATE TABLE run_event (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id       text        NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    seq          integer     NOT NULL,
    event_type   text        NOT NULL,
    payload_json jsonb       NOT NULL,
    created_at   timestamptz NOT NULL,
    UNIQUE (run_id, seq)
);

CREATE TABLE report (
    run_id     text PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    path       text        NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE completion (
    run_id                        text PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    task_id                       text        NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    session_id                    text        REFERENCES session(id) ON DELETE CASCADE,
    project                       text        NOT NULL,
    repository_path               text        NOT NULL,
    branch                        text,
    base_branch                   text,
    head_commit                   text,
    remote                        text,
    remote_branch                 text,
    pull_request_number           integer,
    pull_request_url              text,
    pull_request_state            text,
    replaced_pull_request_number  integer,
    replaced_pull_request_url     text,
    merge_commit                  text,
    merge_mode                    text,
    merge_method                  text,
    completion_state              text        NOT NULL,
    last_reason_code              text,
    requires_human                boolean     NOT NULL DEFAULT false,
    is_recoverable                boolean     NOT NULL DEFAULT false,
    recommended_action            text,
    validation_summary            text,
    policy_json                   jsonb,
    last_checked_at               timestamptz,
    next_retry_at                 timestamptz,
    retry_count                   integer     NOT NULL DEFAULT 0,
    recovery_count                integer     NOT NULL DEFAULT 0,
    version                       integer     NOT NULL DEFAULT 0,
    created_at                    timestamptz NOT NULL,
    updated_at                    timestamptz NOT NULL,
    review_verdict                text,
    review_run_id                 text,
    review_summary                text
);

CREATE TABLE completion_event (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        text        NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    seq           integer     NOT NULL,
    event_type    text        NOT NULL,
    reason_code   text,
    message       text,
    metadata_json jsonb,
    created_at    timestamptz NOT NULL,
    UNIQUE (run_id, seq)
);

CREATE TABLE completion_validation (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         text        NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    attempt        integer     NOT NULL,
    command        text        NOT NULL,
    exit_code      integer,
    started_at     timestamptz,
    finished_at    timestamptz,
    stdout_summary text,
    stderr_summary text,
    created_at     timestamptz NOT NULL
);

CREATE TABLE proposal (
    id                 text PRIMARY KEY,
    kind               text        NOT NULL,
    project            text        NOT NULL,
    task_id            text        REFERENCES task(id) ON DELETE SET NULL,
    title              text        NOT NULL,
    rationale          text        NOT NULL,
    state              text        NOT NULL,
    risk_level         text        NOT NULL,
    policy_json        jsonb,
    eligibility_json   jsonb,
    plan_json          jsonb,
    evidence_digest    text,
    requires_human     boolean     NOT NULL DEFAULT true,
    last_reason_code   text,
    decided_by         text,
    decision_reason    text,
    dispatched_run_id  text        REFERENCES run(id) ON DELETE SET NULL,
    dispatched_task_id text        REFERENCES task(id) ON DELETE SET NULL,
    version            integer     NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL,
    parameters_json    jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE proposal_event (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    proposal_id   text        NOT NULL REFERENCES proposal(id) ON DELETE CASCADE,
    seq           integer     NOT NULL,
    event_type    text        NOT NULL,
    from_state    text,
    to_state      text,
    actor         text,
    reason_code   text,
    message       text,
    metadata_json jsonb,
    created_at    timestamptz NOT NULL,
    UNIQUE (proposal_id, seq)
);

CREATE TABLE proposal_evidence (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    proposal_id text        NOT NULL REFERENCES proposal(id) ON DELETE CASCADE,
    seq         integer     NOT NULL,
    kind        text        NOT NULL,
    source      text        NOT NULL,
    summary     text,
    observed_at timestamptz NOT NULL,
    is_blocker  boolean     NOT NULL DEFAULT false,
    data_json   jsonb,
    created_at  timestamptz NOT NULL,
    UNIQUE (proposal_id, seq)
);

-- `position` preserves the legacy JSON file's list order, which is load-bearing:
-- the queue is displayed and planned in insertion order.
CREATE TABLE queue_entry (
    id           text PRIMARY KEY,
    task_id      text,
    project      text,
    state        text    NOT NULL,
    reason       text,
    run_id       text,
    added_at     timestamptz,
    evaluated_at timestamptz,
    launched_at  timestamptz,
    position     integer NOT NULL DEFAULT 0
);

CREATE TABLE run_provenance (
    run_id                 text PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    task_id                text        NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    repository_path        text,
    worktree_path          text,
    branch                 text,
    base_branch            text,
    base_sha               text,
    head_sha               text,
    pull_request_number    integer,
    pull_request_url       text,
    pull_request_head_sha  text,
    ci_conclusions_json    jsonb,
    ci_observed_at         timestamptz,
    accepted_sha           text,
    accepted_at            timestamptz,
    deployed_sha           text,
    deployment_environment text,
    deployed_at            timestamptz,
    deployment_verified_at timestamptz,
    created_at             timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL
);

CREATE TABLE provenance_evidence (
    integrity_id        text PRIMARY KEY,
    run_id              text        NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    adapter             text        NOT NULL,
    status              text        NOT NULL,
    candidate_sha       text,
    reported_sha        text,
    native_payload_json jsonb       NOT NULL,
    normalized_json     jsonb       NOT NULL,
    observed_at         timestamptz NOT NULL
);

CREATE TABLE run_provider_route (
    run_id           text PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    providers_json   jsonb       NOT NULL,
    max_attempts     integer     NOT NULL CHECK (max_attempts >= 1),
    selection_reason text        NOT NULL,
    policy_version   text        NOT NULL,
    created_at       timestamptz NOT NULL
);

CREATE TABLE provider_attempt (
    run_id                 text        NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    attempt_number         integer     NOT NULL CHECK (attempt_number >= 1),
    provider_id            text        NOT NULL,
    outcome                text        NOT NULL,
    classification         text,
    disposition            text,
    error_code             text,
    parent_attempt_number  integer,
    started_at             timestamptz NOT NULL,
    completed_at           timestamptz,
    PRIMARY KEY (run_id, attempt_number)
);

CREATE TABLE advisor_proposal (
    id               text PRIMARY KEY,
    kind             text        NOT NULL,
    title            text        NOT NULL,
    body             text        NOT NULL DEFAULT '',
    expected_gain    text,
    effort           text,
    project_ref      text        NOT NULL,
    status           text        NOT NULL DEFAULT 'new',
    promoted_task_id text,
    version          integer     NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL
);

CREATE TABLE audit_run (
    id            text PRIMARY KEY,
    project_ref   text        NOT NULL,
    status        text        NOT NULL DEFAULT 'running',
    checks_json   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    finding_count integer     NOT NULL DEFAULT 0,
    version       integer     NOT NULL DEFAULT 0,
    started_at    timestamptz,
    completed_at  timestamptz,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL
);

CREATE TABLE audit_finding (
    id               text PRIMARY KEY,
    run_id           text        NOT NULL REFERENCES audit_run(id) ON DELETE CASCADE,
    category         text        NOT NULL,
    severity         text        NOT NULL DEFAULT 'info',
    summary          text        NOT NULL DEFAULT '',
    file_path        text,
    loc              text,
    status           text        NOT NULL DEFAULT 'open',
    owner            text        NOT NULL,
    project_ref      text,
    promoted_task_id text,
    version          integer     NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL
);

CREATE TABLE conflict (
    id          text PRIMARY KEY,
    kind        text        NOT NULL,
    source_ref  text        NOT NULL DEFAULT '',
    severity    text        NOT NULL DEFAULT 'sev3',
    status      text        NOT NULL DEFAULT 'open',
    owner       text,
    mitigation  text,
    project_ref text,
    opened_at   timestamptz NOT NULL,
    resolved_at timestamptz,
    version     integer     NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL
);

CREATE TABLE contact (
    id           text PRIMARY KEY,
    display_name text        NOT NULL,
    handle       text        NOT NULL DEFAULT '',
    org          text,
    note         text,
    project_ref  text,
    version      integer     NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE message (
    id          text PRIMARY KEY,
    contact_id  text        NOT NULL REFERENCES contact(id) ON DELETE CASCADE,
    direction   text        NOT NULL DEFAULT 'inbound',
    kind        text        NOT NULL DEFAULT 'note',
    body        text        NOT NULL DEFAULT '',
    project_ref text,
    created_at  timestamptz NOT NULL
);

CREATE TABLE networking_invitation (
    id           text PRIMARY KEY,
    contact_id   text        NOT NULL REFERENCES contact(id) ON DELETE CASCADE,
    council_ref  text        NOT NULL,
    status       text        NOT NULL DEFAULT 'pending',
    note         text,
    project_ref  text,
    invited_at   timestamptz NOT NULL,
    responded_at timestamptz,
    version      integer     NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE motion (
    id           text PRIMARY KEY,
    title        text        NOT NULL,
    body         text        NOT NULL DEFAULT '',
    proposed_by  text        NOT NULL,
    quorum       integer     NOT NULL DEFAULT 1,
    project_ref  text,
    proposal_ref text,
    source_ref   text,
    status       text        NOT NULL DEFAULT 'open',
    opened_at    timestamptz NOT NULL,
    decided_at   timestamptz,
    version      integer     NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE council_vote (
    id         text PRIMARY KEY,
    motion_id  text        NOT NULL REFERENCES motion(id) ON DELETE CASCADE,
    voter_id   text        NOT NULL,
    voter_kind text        NOT NULL DEFAULT 'ai',
    role       text        NOT NULL,
    choice     text        NOT NULL,
    rationale  text,
    created_at timestamptz NOT NULL,
    UNIQUE (motion_id, voter_id)
);

CREATE TABLE council_decision (
    motion_id  text PRIMARY KEY REFERENCES motion(id) ON DELETE CASCADE,
    id         text        NOT NULL,
    outcome    text        NOT NULL,
    tally_json jsonb       NOT NULL DEFAULT '{}'::jsonb,
    roles_json jsonb       NOT NULL DEFAULT '[]'::jsonb,
    rationale  text        NOT NULL DEFAULT '',
    quorum     integer     NOT NULL DEFAULT 1,
    decided_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE council_event (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    motion_id     text        NOT NULL REFERENCES motion(id) ON DELETE CASCADE,
    seq           integer     NOT NULL,
    event_type    text        NOT NULL,
    actor         text,
    role          text,
    message       text,
    metadata_json jsonb,
    created_at    timestamptz NOT NULL,
    UNIQUE (motion_id, seq)
);

CREATE TABLE digest_item (
    id          text PRIMARY KEY,
    title       text        NOT NULL,
    body        text        NOT NULL DEFAULT '',
    category    text,
    refs_json   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    created_at  timestamptz NOT NULL,
    day         text,
    position    integer     NOT NULL DEFAULT 0,
    project_ref text
);

CREATE TABLE market_item (
    id           text PRIMARY KEY,
    name         text        NOT NULL,
    kind         text        NOT NULL,
    version      text        NOT NULL DEFAULT '',
    publisher    text        NOT NULL DEFAULT '',
    description  text        NOT NULL DEFAULT '',
    status       text        NOT NULL DEFAULT 'listed',
    provenance   text        NOT NULL DEFAULT '',
    lock_version integer     NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE market_install_log (
    id            text PRIMARY KEY,
    item_id       text        NOT NULL REFERENCES market_item(id) ON DELETE CASCADE,
    actor         text        NOT NULL,
    version       text        NOT NULL DEFAULT '',
    kind          text        NOT NULL,
    provenance    text        NOT NULL DEFAULT '',
    installer     text        NOT NULL DEFAULT '',
    detail        text        NOT NULL DEFAULT '',
    metadata_json jsonb       NOT NULL DEFAULT '{}'::jsonb,
    installed_at  timestamptz NOT NULL,
    created_at    timestamptz NOT NULL
);

CREATE TABLE model_entry (
    id                text PRIMARY KEY,
    name              text        NOT NULL DEFAULT '',
    kind              text        NOT NULL DEFAULT 'external',
    provider          text,
    status            text        NOT NULL DEFAULT 'available',
    cost              double precision,
    quality           double precision,
    latency_ms        integer,
    provenance        text,
    download_progress integer     NOT NULL DEFAULT 0,
    version           integer     NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL
);

CREATE TABLE model_event (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_id      text        NOT NULL REFERENCES model_entry(id) ON DELETE CASCADE,
    seq           integer     NOT NULL,
    action        text        NOT NULL,
    actor         text,
    target_ref    text,
    provenance    text,
    metadata_json jsonb,
    created_at    timestamptz NOT NULL,
    UNIQUE (model_id, seq)
);

CREATE TABLE owner_item (
    id          text PRIMARY KEY,
    title       text        NOT NULL,
    detail      text,
    due         text,
    done        boolean     NOT NULL DEFAULT false,
    source_ref  text,
    version     integer     NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL,
    project_ref text
);

CREATE INDEX idx_session_task_id ON session(task_id);
CREATE INDEX idx_run_session_id ON run(session_id);
CREATE INDEX idx_run_state ON run(state);
CREATE INDEX idx_run_task_id ON run(task_id);
CREATE INDEX idx_run_event_run_id ON run_event(run_id);
CREATE INDEX idx_completion_state ON completion(completion_state);
CREATE INDEX idx_completion_task_id ON completion(task_id);
CREATE INDEX idx_completion_event_run_id ON completion_event(run_id);
CREATE INDEX idx_completion_validation_run_id ON completion_validation(run_id);
CREATE INDEX idx_proposal_project ON proposal(project);
CREATE INDEX idx_proposal_state ON proposal(state);
CREATE INDEX idx_proposal_task_id ON proposal(task_id);
CREATE INDEX idx_proposal_event_proposal_id ON proposal_event(proposal_id);
CREATE INDEX idx_proposal_evidence_proposal_id ON proposal_evidence(proposal_id);
CREATE INDEX idx_queue_entry_state ON queue_entry(state);
CREATE INDEX idx_queue_entry_task ON queue_entry(task_id);
CREATE INDEX idx_run_provenance_pr ON run_provenance(pull_request_number);
CREATE INDEX idx_run_provenance_task_id ON run_provenance(task_id);
CREATE INDEX idx_provenance_evidence_run_id ON provenance_evidence(run_id);
CREATE INDEX idx_provider_attempt_run_id ON provider_attempt(run_id, attempt_number);
CREATE INDEX idx_advisor_proposal_project ON advisor_proposal(project_ref);
CREATE INDEX idx_advisor_proposal_status ON advisor_proposal(status);
CREATE INDEX idx_audit_run_project ON audit_run(project_ref);
CREATE INDEX idx_audit_run_status ON audit_run(status);
CREATE INDEX idx_audit_finding_owner ON audit_finding(owner);
CREATE INDEX idx_audit_finding_project ON audit_finding(project_ref);
CREATE INDEX idx_audit_finding_run ON audit_finding(run_id);
CREATE INDEX idx_audit_finding_status ON audit_finding(status);
CREATE INDEX idx_conflict_kind ON conflict(kind);
CREATE INDEX idx_conflict_owner ON conflict(owner);
CREATE INDEX idx_conflict_project ON conflict(project_ref);
CREATE INDEX idx_conflict_source_ref ON conflict(source_ref);
CREATE INDEX idx_conflict_status ON conflict(status);
CREATE INDEX idx_contact_handle ON contact(handle);
CREATE INDEX idx_contact_project ON contact(project_ref);
CREATE INDEX idx_message_contact ON message(contact_id);
CREATE INDEX idx_message_kind ON message(kind);
CREATE INDEX idx_message_project ON message(project_ref);
CREATE INDEX idx_networking_invitation_contact ON networking_invitation(contact_id);
CREATE INDEX idx_networking_invitation_council_ref ON networking_invitation(council_ref);
CREATE INDEX idx_networking_invitation_project ON networking_invitation(project_ref);
CREATE INDEX idx_networking_invitation_status ON networking_invitation(status);
CREATE INDEX idx_motion_project ON motion(project_ref);
CREATE INDEX idx_motion_source_ref ON motion(source_ref);
CREATE INDEX idx_motion_status ON motion(status);
CREATE INDEX idx_council_vote_motion ON council_vote(motion_id);
CREATE INDEX idx_council_decision_created ON council_decision(created_at);
CREATE INDEX idx_council_decision_outcome ON council_decision(outcome);
CREATE INDEX idx_council_event_motion ON council_event(motion_id);
CREATE INDEX idx_digest_item_category ON digest_item(category);
CREATE INDEX idx_digest_item_created ON digest_item(created_at);
CREATE INDEX idx_digest_item_day ON digest_item(day);
CREATE INDEX idx_digest_item_project ON digest_item(project_ref);
CREATE INDEX idx_market_item_kind ON market_item(kind);
CREATE INDEX idx_market_item_publisher ON market_item(publisher);
CREATE INDEX idx_market_item_status ON market_item(status);
CREATE INDEX idx_market_install_log_item ON market_install_log(item_id);
CREATE INDEX idx_model_entry_kind ON model_entry(kind);
CREATE INDEX idx_model_entry_status ON model_entry(status);
CREATE INDEX idx_model_event_model_id ON model_event(model_id);
CREATE INDEX idx_owner_item_done ON owner_item(done);
CREATE INDEX idx_owner_item_project ON owner_item(project_ref);
