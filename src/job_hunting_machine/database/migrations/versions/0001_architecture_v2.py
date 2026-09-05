"""Create Architecture v2 application-owned schema.

Revision ID: 0001_architecture_v2
Revises: None

The SQL below is frozen from Architecture v2 sections 10 and 12-33. Primary keys
are explicitly NOT NULL to implement durable identity despite SQLite's nullable
TEXT PRIMARY KEY compatibility behavior. Two triggers enforce section 19's
append-only audit history. This migration never imports live ORM metadata.
"""

from alembic import op

revision: str = "0001_architecture_v2"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_SCHEMA: tuple[str, ...] = (
    """
CREATE TABLE qualification_rule_sets (
    rule_set_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    description TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1)),
    effective_from TEXT,
    effective_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, version)
);
""",
    """
CREATE TABLE qualification_rules (
    rule_id TEXT PRIMARY KEY NOT NULL,
    rule_set_id TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    description TEXT NOT NULL,

    applies_to TEXT NOT NULL
        CHECK (
            applies_to IN (
                'ALL',
                'INTERNSHIP',
                'NEW_GRAD',
                'EARLY_CAREER',
                'FULL_TIME'
            )
        ),

    evaluator_kind TEXT NOT NULL
        CHECK (
            evaluator_kind IN (
                'DETERMINISTIC',
                'SEMANTIC',
                'RESEARCH'
            )
        ),

    severity TEXT NOT NULL
        CHECK (severity IN ('HARD', 'SOFT')),

    operator TEXT,
    expected_value_json TEXT
        CHECK (
            expected_value_json IS NULL
            OR json_valid(expected_value_json)
        ),

    priority INTEGER NOT NULL DEFAULT 100,

    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1)),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(rule_set_id)
        REFERENCES qualification_rule_sets(rule_set_id),

    UNIQUE(rule_set_id, rule_key)
);
""",
    """
CREATE TABLE salary_location_rules (
    salary_rule_id TEXT PRIMARY KEY NOT NULL,

    country_code TEXT NOT NULL,
    state_region TEXT,
    city TEXT,

    cost_tier TEXT NOT NULL
        CHECK (
            cost_tier IN (
                'STANDARD',
                'HIGH',
                'VERY_HIGH'
            )
        ),

    internship_min_hourly_usd REAL NOT NULL,

    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1)),

    notes TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
""",
    """
CREATE TABLE check_job_position_quality (
    job_id TEXT PRIMARY KEY NOT NULL,

    original_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,

    url_sha256 TEXT NOT NULL,

    source_type TEXT NOT NULL,
    source_reference TEXT,

    company_name TEXT,
    job_title TEXT,

    employment_type TEXT,
    location_text TEXT,
    country_code TEXT,
    city TEXT,
    state_region TEXT,

    salary_min REAL,
    salary_max REAL,
    salary_currency TEXT,
    salary_period TEXT,

    required_experience_min REAL,
    required_experience_max REAL,

    posting_date TEXT,
    application_deadline TEXT,

    internship_start TEXT,
    internship_end TEXT,

    work_authorization_text TEXT,

    description_text_path TEXT,
    raw_snapshot_path TEXT,
    content_sha256 TEXT,

    qualification_status TEXT NOT NULL
        CHECK (
            qualification_status IN (
                'NEW',
                'ACTIVE',
                'PASSED',
                'ABORTED',
                'NEEDS_REVIEW',
                'ERROR'
            )
        ),

    qualification_confidence REAL,

    failed_rules_json TEXT
        CHECK (
            failed_rules_json IS NULL
            OR json_valid(failed_rules_json)
        ),

    unknown_rules_json TEXT
        CHECK (
            unknown_rules_json IS NULL
            OR json_valid(unknown_rules_json)
        ),

    notes TEXT,

    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    updated_at TEXT NOT NULL,

    UNIQUE(canonical_url)
);
""",
    """
CREATE TABLE application_pipeline (
    application_id TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL UNIQUE,

    pipeline_stage TEXT NOT NULL
        CHECK (
            pipeline_stage IN (
                'QUALIFICATION',
                'RESUME',
                'FORM',
                'REVIEW',
                'SUBMISSION',
                'POST_SUBMISSION',
                'MONITORING',
                'CLOSED'
            )
        ),

    application_status TEXT NOT NULL,

    priority INTEGER NOT NULL DEFAULT 100,

    current_task_id TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,

    FOREIGN KEY(job_id)
        REFERENCES check_job_position_quality(job_id)
);
""",
    """
CREATE TABLE application_details (
    application_id TEXT PRIMARY KEY NOT NULL,

    company_name TEXT NOT NULL,
    job_title TEXT NOT NULL,

    job_url TEXT NOT NULL,
    application_url TEXT,

    ats_type TEXT,

    portal_account_identifier TEXT,

    location_text TEXT,
    employment_type TEXT,

    application_status TEXT NOT NULL,

    submitted_at TEXT,
    last_status_checked_at TEXT,

    resume_artifact_id TEXT,
    cover_letter_artifact_id TEXT,

    referral_contact_status TEXT NOT NULL DEFAULT 'NOT_STARTED',

    notes TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(application_id) REFERENCES application_pipeline(application_id) ON DELETE CASCADE
);
""",
    """
CREATE TABLE agent_queue (
    task_id TEXT PRIMARY KEY NOT NULL,

    application_id TEXT,
    job_id TEXT,

    parent_task_id TEXT,

    task_type TEXT NOT NULL,

    task_status TEXT NOT NULL
        CHECK (
            task_status IN (
                'NEW',
                'READY',
                'ACTIVE',
                'WAITING_HUMAN',
                'WAITING_RETRY',
                'SUCCEEDED',
                'ABORTED',
                'FAILED',
                'CANCELED'
            )
        ),

    priority INTEGER NOT NULL DEFAULT 100,

    payload_json TEXT
        CHECK (
            payload_json IS NULL
            OR json_valid(payload_json)
        ),

    dedupe_key TEXT,

    checkpoint_key TEXT,

    worker_id TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,

    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,

    next_run_at TEXT,

    last_error_code TEXT,
    last_error_message TEXT,

    version INTEGER NOT NULL DEFAULT 1,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id),

    FOREIGN KEY(job_id)
        REFERENCES check_job_position_quality(job_id),

    FOREIGN KEY(parent_task_id)
        REFERENCES agent_queue(task_id),

    UNIQUE(dedupe_key)
);
""",
    """
CREATE TABLE task_memory (
    task_id TEXT PRIMARY KEY NOT NULL,

    memory_version INTEGER NOT NULL DEFAULT 1,

    summary TEXT,

    state_json TEXT NOT NULL
        CHECK (json_valid(state_json)),

    last_checkpoint TEXT,

    last_agent TEXT,

    updated_at TEXT NOT NULL,

    FOREIGN KEY(task_id) REFERENCES agent_queue(task_id) ON DELETE CASCADE
);
""",
    """
CREATE TABLE activity_log (
    event_id TEXT PRIMARY KEY NOT NULL,

    task_id TEXT,
    application_id TEXT,
    job_id TEXT,

    actor_type TEXT NOT NULL
        CHECK (
            actor_type IN (
                'SYSTEM',
                'AGENT',
                'USER',
                'MODEL',
                'SLACK',
                'BROWSER'
            )
        ),

    actor_name TEXT,

    event_type TEXT NOT NULL,

    old_state TEXT,
    new_state TEXT,

    message TEXT,

    metadata_json TEXT
        CHECK (
            metadata_json IS NULL
            OR json_valid(metadata_json)
        ),

    created_at TEXT NOT NULL,

    FOREIGN KEY(task_id)
        REFERENCES agent_queue(task_id),

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id),

    FOREIGN KEY(job_id)
        REFERENCES check_job_position_quality(job_id)
);
""",
    """
CREATE TABLE slack_events (
    slack_event_id TEXT PRIMARY KEY NOT NULL,

    event_type TEXT NOT NULL,

    channel_id TEXT,
    message_ts TEXT,
    thread_ts TEXT,
    user_id TEXT,

    payload_sha256 TEXT NOT NULL,

    processing_status TEXT NOT NULL,

    received_at TEXT NOT NULL,
    processed_at TEXT,

    UNIQUE(channel_id, message_ts, event_type)
);
""",
    """
CREATE TABLE my_information_for_filling_form (
    info_key TEXT PRIMARY KEY NOT NULL,

    category TEXT NOT NULL,

    value_json TEXT NOT NULL
        CHECK (json_valid(value_json)),

    value_type TEXT NOT NULL,

    sensitivity TEXT NOT NULL
        CHECK (
            sensitivity IN (
                'NORMAL',
                'PERSONAL',
                'SENSITIVE'
            )
        ),

    auto_fill_policy TEXT NOT NULL
        CHECK (
            auto_fill_policy IN (
                'ALLOW',
                'ASK_IF_AMBIGUOUS',
                'MANUAL_ONLY'
            )
        ),

    source TEXT NOT NULL,

    verified INTEGER NOT NULL DEFAULT 0
        CHECK (verified IN (0, 1)),

    notes TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
""",
    """
CREATE TABLE candidate_facts (
    fact_id TEXT PRIMARY KEY NOT NULL,

    fact_type TEXT NOT NULL,
    fact_key TEXT NOT NULL,

    value_json TEXT NOT NULL
        CHECK (json_valid(value_json)),

    source_type TEXT NOT NULL,
    source_reference TEXT NOT NULL,

    evidence_path TEXT,

    verification_status TEXT NOT NULL
        CHECK (
            verification_status IN (
                'VERIFIED',
                'UNVERIFIED',
                'REJECTED'
            )
        ),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    UNIQUE(fact_key, source_reference)
);
""",
    """
CREATE TABLE project_catalog (
    project_id TEXT PRIMARY KEY NOT NULL,

    name TEXT NOT NULL,

    github_url TEXT,
    repository_name TEXT,

    description TEXT,

    languages_json TEXT
        CHECK (
            languages_json IS NULL
            OR json_valid(languages_json)
        ),

    frameworks_json TEXT
        CHECK (
            frameworks_json IS NULL
            OR json_valid(frameworks_json)
        ),

    topics_json TEXT
        CHECK (
            topics_json IS NULL
            OR json_valid(topics_json)
        ),

    verified_facts_json TEXT
        CHECK (
            verified_facts_json IS NULL
            OR json_valid(verified_facts_json)
        ),

    evidence_path TEXT,

    source_commit_sha TEXT,

    last_scanned_at TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
""",
    """
CREATE TABLE skill_catalog (
    skill_id TEXT PRIMARY KEY NOT NULL,

    canonical_name TEXT NOT NULL UNIQUE,

    category TEXT NOT NULL,

    evidence_json TEXT NOT NULL
        CHECK (json_valid(evidence_json)),

    verification_status TEXT NOT NULL,

    last_verified_at TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
""",
    """
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY NOT NULL,

    application_id TEXT,

    task_id TEXT,

    artifact_type TEXT NOT NULL
        CHECK (
            artifact_type IN (
                'RESUME_DOCX',
                'RESUME_PDF',
                'COVER_LETTER_DOCX',
                'COVER_LETTER_PDF',
                'TRANSCRIPT',
                'SCREENSHOT',
                'JOB_SNAPSHOT',
                'OTHER'
            )
        ),

    path TEXT NOT NULL,

    sha256 TEXT NOT NULL,

    mime_type TEXT,

    version INTEGER NOT NULL DEFAULT 1,

    approved_for_submission INTEGER NOT NULL DEFAULT 0
        CHECK (approved_for_submission IN (0, 1)),

    created_at TEXT NOT NULL,

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id),

    FOREIGN KEY(task_id)
        REFERENCES agent_queue(task_id)
);
""",
    """
CREATE TABLE form_answers (
    answer_id TEXT PRIMARY KEY NOT NULL,

    application_id TEXT NOT NULL,

    page_key TEXT,
    field_key TEXT NOT NULL,

    question_text TEXT NOT NULL,

    answer_json TEXT
        CHECK (
            answer_json IS NULL
            OR json_valid(answer_json)
        ),

    answer_source_type TEXT
        CHECK (
            answer_source_type IN (
                'CANONICAL_INFO',
                'CANDIDATE_FACT',
                'USER',
                'DERIVED',
                'FILE'
            )
        ),

    answer_source_reference TEXT,

    confidence REAL,

    answer_status TEXT NOT NULL
        CHECK (
            answer_status IN (
                'EMPTY',
                'FILLED',
                'NEEDS_USER',
                'VALIDATED'
            )
        ),

    last_verified_at TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id),

    UNIQUE(application_id, page_key, field_key)
);
""",
    """
CREATE TABLE browser_sessions (
    browser_session_id TEXT PRIMARY KEY NOT NULL,

    application_id TEXT NOT NULL,

    ats_type TEXT,

    storage_state_path TEXT,

    current_url TEXT,

    current_page_key TEXT,

    session_status TEXT NOT NULL,

    expires_at TEXT,

    last_checkpoint_at TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id)
);
""",
    """
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY NOT NULL,

    application_id TEXT,
    task_id TEXT,

    approval_type TEXT NOT NULL
        CHECK (
            approval_type IN (
                'PREPARE_APPLICATION',
                'SUBMIT_APPLICATION',
                'SEND_EMAIL',
                'SEND_EXTERNAL_MESSAGE'
            )
        ),

    approval_status TEXT NOT NULL
        CHECK (
            approval_status IN (
                'PENDING',
                'APPROVED',
                'REJECTED',
                'EXPIRED',
                'REVOKED',
                'CONSUMED'
            )
        ),

    payload_path TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,

    requested_via TEXT NOT NULL DEFAULT 'SLACK',

    slack_channel_id TEXT,
    slack_message_ts TEXT,

    decided_by_slack_user_id TEXT,

    requested_at TEXT NOT NULL,
    decided_at TEXT,

    valid_until TEXT,
    consumed_at TEXT,

    rejection_reason TEXT,

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id),

    FOREIGN KEY(task_id)
        REFERENCES agent_queue(task_id)
);
""",
    """
CREATE TABLE external_actions (
    external_action_id TEXT PRIMARY KEY NOT NULL,

    application_id TEXT,
    task_id TEXT,
    approval_id TEXT,

    action_type TEXT NOT NULL,

    idempotency_key TEXT NOT NULL UNIQUE,

    request_sha256 TEXT NOT NULL,

    action_status TEXT NOT NULL
        CHECK (
            action_status IN (
                'PLANNED',
                'EXECUTING',
                'SUCCEEDED',
                'FAILED',
                'UNKNOWN_RESULT'
            )
        ),

    external_reference TEXT,

    result_json TEXT
        CHECK (
            result_json IS NULL
            OR json_valid(result_json)
        ),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id),

    FOREIGN KEY(task_id)
        REFERENCES agent_queue(task_id),

    FOREIGN KEY(approval_id)
        REFERENCES approvals(approval_id)
);
""",
    """
CREATE TABLE contacts (
    contact_id TEXT PRIMARY KEY NOT NULL,

    application_id TEXT NOT NULL,

    company_name TEXT NOT NULL,

    full_name TEXT,

    title TEXT,

    contact_type TEXT
        CHECK (
            contact_type IN (
                'RECRUITER',
                'HR',
                'HIRING_MANAGER',
                'EMPLOYEE',
                'OTHER'
            )
        ),

    email TEXT,
    linkedin_url TEXT,

    source_url TEXT,

    confidence REAL,

    verified INTEGER NOT NULL DEFAULT 0
        CHECK (verified IN (0, 1)),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id)
);
""",
    """
CREATE TABLE outreach_drafts (
    outreach_id TEXT PRIMARY KEY NOT NULL,

    application_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,

    channel TEXT NOT NULL
        CHECK (
            channel IN (
                'EMAIL',
                'LINKEDIN_MANUAL'
            )
        ),

    subject TEXT,
    body TEXT NOT NULL,

    payload_sha256 TEXT NOT NULL,

    draft_status TEXT NOT NULL
        CHECK (
            draft_status IN (
                'DRAFT',
                'READY_FOR_REVIEW',
                'APPROVED',
                'SENT',
                'REJECTED'
            )
        ),

    approval_id TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id),

    FOREIGN KEY(contact_id)
        REFERENCES contacts(contact_id),

    FOREIGN KEY(approval_id)
        REFERENCES approvals(approval_id)
);
""",
    """
CREATE TABLE monitor_events (
    monitor_event_id TEXT PRIMARY KEY NOT NULL,

    application_id TEXT NOT NULL,

    source_type TEXT NOT NULL
        CHECK (
            source_type IN (
                'PORTAL',
                'EMAIL'
            )
        ),

    source_reference TEXT,

    detected_status TEXT,

    previous_status TEXT,

    confidence REAL,

    evidence_path TEXT,

    meaningful_change INTEGER NOT NULL
        CHECK (meaningful_change IN (0, 1)),

    observed_at TEXT NOT NULL,

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id)
);
""",
    """
CREATE TABLE model_usage (
    model_usage_id TEXT PRIMARY KEY NOT NULL,

    task_id TEXT,
    application_id TEXT,

    agent_name TEXT NOT NULL,
    operation TEXT NOT NULL,

    model_id TEXT NOT NULL,
    reasoning_effort TEXT,

    prompt_version TEXT,
    prompt_sha256 TEXT,

    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,

    estimated_cost_usd REAL,

    response_id TEXT,

    success INTEGER NOT NULL
        CHECK (success IN (0, 1)),

    created_at TEXT NOT NULL,

    FOREIGN KEY(task_id)
        REFERENCES agent_queue(task_id),

    FOREIGN KEY(application_id)
        REFERENCES application_pipeline(application_id)
);
""",
    """CREATE INDEX idx_agent_queue_claim
ON agent_queue(task_status, next_run_at, priority, created_at);""",
    """CREATE INDEX idx_agent_queue_application
ON agent_queue(application_id);""",
    """CREATE TRIGGER activity_log_no_replace
BEFORE INSERT ON activity_log
WHEN EXISTS (SELECT 1 FROM activity_log WHERE event_id = NEW.event_id)
BEGIN
    SELECT RAISE(ABORT, 'activity_log is append-only');
END;""",
    """CREATE TRIGGER activity_log_no_update
BEFORE UPDATE ON activity_log
BEGIN
    SELECT RAISE(ABORT, 'activity_log is append-only');
END;""",
    """CREATE TRIGGER activity_log_no_delete
BEFORE DELETE ON activity_log
BEGIN
    SELECT RAISE(ABORT, 'activity_log is append-only');
END;""",
)


def upgrade() -> None:
    """Create all domain tables atomically in the caller-owned transaction."""
    for statement in _SCHEMA:
        op.execute(statement)


def downgrade() -> None:
    """Remove the initial schema; only use against intentionally disposable data."""
    op.execute("DROP TRIGGER activity_log_no_replace")
    op.execute("DROP TRIGGER activity_log_no_delete")
    op.execute("DROP TRIGGER activity_log_no_update")
    op.drop_table("model_usage")
    op.drop_table("monitor_events")
    op.drop_table("outreach_drafts")
    op.drop_table("contacts")
    op.drop_table("external_actions")
    op.drop_table("approvals")
    op.drop_table("browser_sessions")
    op.drop_table("form_answers")
    op.drop_table("artifacts")
    op.drop_table("skill_catalog")
    op.drop_table("project_catalog")
    op.drop_table("candidate_facts")
    op.drop_table("my_information_for_filling_form")
    op.drop_table("slack_events")
    op.drop_table("activity_log")
    op.drop_table("task_memory")
    op.drop_table("agent_queue")
    op.drop_table("application_details")
    op.drop_table("application_pipeline")
    op.drop_table("check_job_position_quality")
    op.drop_table("salary_location_rules")
    op.drop_table("qualification_rules")
    op.drop_table("qualification_rule_sets")
