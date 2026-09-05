"""Typed mappings of every Architecture v2 application-owned table.

Alembic owns schema creation. These mappings never create tables on import and
have no ID/time defaults: repositories must generate those once per write boundary.
Date-only job information remains plain text; operational timestamps use UTCText.
"""

from sqlalchemy import (
    REAL,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from job_hunting_machine.database.schema_types import IdText, UTCText
from job_hunting_machine.ids import IdKind


class Base(DeclarativeBase):
    """Shared metadata for application-owned tables, never LangGraph internals."""


class QualificationRuleSet(Base):
    """Architecture v2 `qualification_rule_sets` row."""

    __tablename__ = "qualification_rule_sets"
    __table_args__ = (
        UniqueConstraint("name", "version"),
        CheckConstraint("enabled IN (0, 1)"),
    )

    rule_set_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    effective_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_until: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class QualificationRule(Base):
    """Architecture v2 `qualification_rules` row."""

    __tablename__ = "qualification_rules"
    __table_args__ = (
        UniqueConstraint("rule_set_id", "rule_key"),
        CheckConstraint(
            " applies_to IN ( 'ALL', 'INTERNSHIP', 'NEW_GRAD', 'EARLY_CAREER', 'FULL_TIME' ) "
        ),
        CheckConstraint(" evaluator_kind IN ( 'DETERMINISTIC', 'SEMANTIC', 'RESEARCH' ) "),
        CheckConstraint("severity IN ('HARD', 'SOFT')"),
        CheckConstraint(" expected_value_json IS NULL OR json_valid(expected_value_json) "),
        CheckConstraint("enabled IN (0, 1)"),
    )

    rule_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    rule_set_id: Mapped[str] = mapped_column(
        Text, ForeignKey("qualification_rule_sets.rule_set_id"), nullable=False
    )
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    applies_to: Mapped[str] = mapped_column(Text, nullable=False)
    evaluator_kind: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class SalaryLocationRule(Base):
    """Architecture v2 `salary_location_rules` row."""

    __tablename__ = "salary_location_rules"
    __table_args__ = (
        CheckConstraint(" cost_tier IN ( 'STANDARD', 'HIGH', 'VERY_HIGH' ) "),
        CheckConstraint("enabled IN (0, 1)"),
    )

    salary_rule_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    country_code: Mapped[str] = mapped_column(Text, nullable=False)
    state_region: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_tier: Mapped[str] = mapped_column(Text, nullable=False)
    internship_min_hourly_usd: Mapped[float] = mapped_column(REAL, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class Job(Base):
    """Architecture v2 `check_job_position_quality` row."""

    __tablename__ = "check_job_position_quality"
    __table_args__ = (
        UniqueConstraint("canonical_url"),
        CheckConstraint(
            " qualification_status IN ( 'NEW', 'ACTIVE', 'PASSED', 'ABORTED', "
            "'NEEDS_REVIEW', 'ERROR' ) "
        ),
        CheckConstraint(" failed_rules_json IS NULL OR json_valid(failed_rules_json) "),
        CheckConstraint(" unknown_rules_json IS NULL OR json_valid(unknown_rules_json) "),
    )

    job_id: Mapped[str] = mapped_column(IdText(IdKind.JOB), primary_key=True, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    location_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    country_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_region: Mapped[str | None] = mapped_column(Text, nullable=True)
    salary_min: Mapped[float | None] = mapped_column(REAL, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(REAL, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(Text, nullable=True)
    salary_period: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_experience_min: Mapped[float | None] = mapped_column(REAL, nullable=True)
    required_experience_max: Mapped[float | None] = mapped_column(REAL, nullable=True)
    posting_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_deadline: Mapped[str | None] = mapped_column(Text, nullable=True)
    internship_start: Mapped[str | None] = mapped_column(Text, nullable=True)
    internship_end: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_authorization_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_text_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_snapshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualification_status: Mapped[str] = mapped_column(Text, nullable=False)
    qualification_confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    failed_rules_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    unknown_rules_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    last_checked_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class ApplicationPipeline(Base):
    """Architecture v2 `application_pipeline` row."""

    __tablename__ = "application_pipeline"
    __table_args__ = (
        UniqueConstraint("job_id"),
        CheckConstraint(
            " pipeline_stage IN ( 'QUALIFICATION', 'RESUME', 'FORM', 'REVIEW', "
            "'SUBMISSION', 'POST_SUBMISSION', 'MONITORING', 'CLOSED' ) "
        ),
    )

    application_id: Mapped[str] = mapped_column(
        IdText(IdKind.APPLICATION), primary_key=True, nullable=False
    )
    job_id: Mapped[str] = mapped_column(
        IdText(IdKind.JOB), ForeignKey("check_job_position_quality.job_id"), nullable=False
    )
    pipeline_stage: Mapped[str] = mapped_column(Text, nullable=False)
    application_status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    current_task_id: Mapped[str | None] = mapped_column(IdText(IdKind.TASK), nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    closed_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)


class ApplicationDetails(Base):
    """Architecture v2 `application_details` row."""

    __tablename__ = "application_details"

    application_id: Mapped[str] = mapped_column(
        IdText(IdKind.APPLICATION),
        ForeignKey("application_pipeline.application_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    job_title: Mapped[str] = mapped_column(Text, nullable=False)
    job_url: Mapped[str] = mapped_column(Text, nullable=False)
    application_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ats_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    portal_account_identifier: Mapped[str | None] = mapped_column(Text, nullable=True)
    location_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_status: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    last_status_checked_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    resume_artifact_id: Mapped[str | None] = mapped_column(IdText(IdKind.ARTIFACT), nullable=True)
    cover_letter_artifact_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.ARTIFACT), nullable=True
    )
    referral_contact_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'NOT_STARTED'")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class AgentTask(Base):
    """Architecture v2 `agent_queue` row."""

    __tablename__ = "agent_queue"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        CheckConstraint(
            " task_status IN ( 'NEW', 'READY', 'ACTIVE', 'WAITING_HUMAN', 'WAITING_RETRY', "
            "'SUCCEEDED', 'ABORTED', 'FAILED', 'CANCELED' ) "
        ),
        CheckConstraint(" payload_json IS NULL OR json_valid(payload_json) "),
        Index("idx_agent_queue_claim", "task_status", "next_run_at", "priority", "created_at"),
        Index("idx_agent_queue_application", "application_id"),
    )

    task_id: Mapped[str] = mapped_column(IdText(IdKind.TASK), primary_key=True, nullable=False)
    application_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.APPLICATION), ForeignKey("application_pipeline.application_id"), nullable=True
    )
    job_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.JOB), ForeignKey("check_job_position_quality.job_id"), nullable=True
    )
    parent_task_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.TASK), ForeignKey("agent_queue.task_id"), nullable=True
    )
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    task_status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    next_run_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    started_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)

    # SQLAlchemy requires this declarative mapping; it is not per-instance state.
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class TaskMemory(Base):
    """Architecture v2 `task_memory` row."""

    __tablename__ = "task_memory"
    __table_args__ = (CheckConstraint("json_valid(state_json)"),)

    task_id: Mapped[str] = mapped_column(
        IdText(IdKind.TASK),
        ForeignKey("agent_queue.task_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    memory_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    last_checkpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class ActivityLog(Base):
    """Architecture v2 `activity_log` row."""

    __tablename__ = "activity_log"
    __table_args__ = (
        CheckConstraint(
            " actor_type IN ( 'SYSTEM', 'AGENT', 'USER', 'MODEL', 'SLACK', 'BROWSER' ) "
        ),
        CheckConstraint(" metadata_json IS NULL OR json_valid(metadata_json) "),
    )

    event_id: Mapped[str] = mapped_column(IdText(IdKind.EVENT), primary_key=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.TASK), ForeignKey("agent_queue.task_id"), nullable=True
    )
    application_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.APPLICATION), ForeignKey("application_pipeline.application_id"), nullable=True
    )
    job_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.JOB), ForeignKey("check_job_position_quality.job_id"), nullable=True
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    old_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class SlackEvent(Base):
    """Architecture v2 `slack_events` row."""

    __tablename__ = "slack_events"
    __table_args__ = (UniqueConstraint("channel_id", "message_ts", "event_type"),)

    slack_event_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_ts: Mapped[str | None] = mapped_column(Text, nullable=True)
    thread_ts: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    processing_status: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    processed_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)


class FormInformation(Base):
    """Architecture v2 `my_information_for_filling_form` row."""

    __tablename__ = "my_information_for_filling_form"
    __table_args__ = (
        CheckConstraint("json_valid(value_json)"),
        CheckConstraint(" sensitivity IN ( 'NORMAL', 'PERSONAL', 'SENSITIVE' ) "),
        CheckConstraint(" auto_fill_policy IN ( 'ALLOW', 'ASK_IF_AMBIGUOUS', 'MANUAL_ONLY' ) "),
        CheckConstraint("verified IN (0, 1)"),
    )

    info_key: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(Text, nullable=False)
    sensitivity: Mapped[str] = mapped_column(Text, nullable=False)
    auto_fill_policy: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    verified: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class CandidateFact(Base):
    """Architecture v2 `candidate_facts` row."""

    __tablename__ = "candidate_facts"
    __table_args__ = (
        UniqueConstraint("fact_key", "source_reference"),
        CheckConstraint("json_valid(value_json)"),
        CheckConstraint(" verification_status IN ( 'VERIFIED', 'UNVERIFIED', 'REJECTED' ) "),
    )

    fact_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    fact_type: Mapped[str] = mapped_column(Text, nullable=False)
    fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class ProjectCatalog(Base):
    """Architecture v2 `project_catalog` row."""

    __tablename__ = "project_catalog"
    __table_args__ = (
        CheckConstraint(" languages_json IS NULL OR json_valid(languages_json) "),
        CheckConstraint(" frameworks_json IS NULL OR json_valid(frameworks_json) "),
        CheckConstraint(" topics_json IS NULL OR json_valid(topics_json) "),
        CheckConstraint(" verified_facts_json IS NULL OR json_valid(verified_facts_json) "),
    )

    project_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    github_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    repository_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    languages_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    frameworks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    topics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_facts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scanned_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class SkillCatalog(Base):
    """Architecture v2 `skill_catalog` row."""

    __tablename__ = "skill_catalog"
    __table_args__ = (
        UniqueConstraint("canonical_name"),
        CheckConstraint("json_valid(evidence_json)"),
    )

    skill_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str] = mapped_column(Text, nullable=False)
    last_verified_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class Artifact(Base):
    """Architecture v2 `artifacts` row."""

    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            " artifact_type IN ( 'RESUME_DOCX', 'RESUME_PDF', 'COVER_LETTER_DOCX', "
            "'COVER_LETTER_PDF', 'TRANSCRIPT', 'SCREENSHOT', 'JOB_SNAPSHOT', 'OTHER' ) "
        ),
        CheckConstraint("approved_for_submission IN (0, 1)"),
    )

    artifact_id: Mapped[str] = mapped_column(
        IdText(IdKind.ARTIFACT), primary_key=True, nullable=False
    )
    application_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.APPLICATION), ForeignKey("application_pipeline.application_id"), nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.TASK), ForeignKey("agent_queue.task_id"), nullable=True
    )
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    approved_for_submission: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class FormAnswer(Base):
    """Architecture v2 `form_answers` row."""

    __tablename__ = "form_answers"
    __table_args__ = (
        UniqueConstraint("application_id", "page_key", "field_key"),
        CheckConstraint(" answer_json IS NULL OR json_valid(answer_json) "),
        CheckConstraint(
            " answer_source_type IN ( 'CANONICAL_INFO', 'CANDIDATE_FACT', "
            "'USER', 'DERIVED', 'FILE' ) "
        ),
        CheckConstraint(" answer_status IN ( 'EMPTY', 'FILLED', 'NEEDS_USER', 'VALIDATED' ) "),
    )

    answer_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    application_id: Mapped[str] = mapped_column(
        IdText(IdKind.APPLICATION),
        ForeignKey("application_pipeline.application_id"),
        nullable=False,
    )
    page_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_source_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    answer_status: Mapped[str] = mapped_column(Text, nullable=False)
    last_verified_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class BrowserSession(Base):
    """Architecture v2 `browser_sessions` row."""

    __tablename__ = "browser_sessions"

    browser_session_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    application_id: Mapped[str] = mapped_column(
        IdText(IdKind.APPLICATION),
        ForeignKey("application_pipeline.application_id"),
        nullable=False,
    )
    ats_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_state_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_page_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    last_checkpoint_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class Approval(Base):
    """Architecture v2 `approvals` row."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            " approval_type IN ( 'PREPARE_APPLICATION', 'SUBMIT_APPLICATION', "
            "'SEND_EMAIL', 'SEND_EXTERNAL_MESSAGE' ) "
        ),
        CheckConstraint(
            " approval_status IN ( 'PENDING', 'APPROVED', 'REJECTED', "
            "'EXPIRED', 'REVOKED', 'CONSUMED' ) "
        ),
    )

    approval_id: Mapped[str] = mapped_column(
        IdText(IdKind.APPROVAL), primary_key=True, nullable=False
    )
    application_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.APPLICATION), ForeignKey("application_pipeline.application_id"), nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.TASK), ForeignKey("agent_queue.task_id"), nullable=True
    )
    approval_type: Mapped[str] = mapped_column(Text, nullable=False)
    approval_status: Mapped[str] = mapped_column(Text, nullable=False)
    payload_path: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    requested_via: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'SLACK'"))
    slack_channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    slack_message_ts: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_slack_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    decided_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    valid_until: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    consumed_at: Mapped[str | None] = mapped_column(UTCText(), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExternalAction(Base):
    """Architecture v2 `external_actions` row."""

    __tablename__ = "external_actions"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint(
            " action_status IN ( 'PLANNED', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'UNKNOWN_RESULT' ) "
        ),
        CheckConstraint(" result_json IS NULL OR json_valid(result_json) "),
    )

    external_action_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    application_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.APPLICATION), ForeignKey("application_pipeline.application_id"), nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.TASK), ForeignKey("agent_queue.task_id"), nullable=True
    )
    approval_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.APPROVAL), ForeignKey("approvals.approval_id"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    action_status: Mapped[str] = mapped_column(Text, nullable=False)
    external_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class Contact(Base):
    """Architecture v2 `contacts` row."""

    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(
            " contact_type IN ( 'RECRUITER', 'HR', 'HIRING_MANAGER', 'EMPLOYEE', 'OTHER' ) "
        ),
        CheckConstraint("verified IN (0, 1)"),
    )

    contact_id: Mapped[str] = mapped_column(
        IdText(IdKind.CONTACT), primary_key=True, nullable=False
    )
    application_id: Mapped[str] = mapped_column(
        IdText(IdKind.APPLICATION),
        ForeignKey("application_pipeline.application_id"),
        nullable=False,
    )
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    verified: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class OutreachDraft(Base):
    """Architecture v2 `outreach_drafts` row."""

    __tablename__ = "outreach_drafts"
    __table_args__ = (
        CheckConstraint(" channel IN ( 'EMAIL', 'LINKEDIN_MANUAL' ) "),
        CheckConstraint(
            " draft_status IN ( 'DRAFT', 'READY_FOR_REVIEW', 'APPROVED', 'SENT', 'REJECTED' ) "
        ),
    )

    outreach_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    application_id: Mapped[str] = mapped_column(
        IdText(IdKind.APPLICATION),
        ForeignKey("application_pipeline.application_id"),
        nullable=False,
    )
    contact_id: Mapped[str] = mapped_column(
        IdText(IdKind.CONTACT), ForeignKey("contacts.contact_id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    draft_status: Mapped[str] = mapped_column(Text, nullable=False)
    approval_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.APPROVAL), ForeignKey("approvals.approval_id"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class MonitorEvent(Base):
    """Architecture v2 `monitor_events` row."""

    __tablename__ = "monitor_events"
    __table_args__ = (
        CheckConstraint(" source_type IN ( 'PORTAL', 'EMAIL' ) "),
        CheckConstraint("meaningful_change IN (0, 1)"),
    )

    monitor_event_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    application_id: Mapped[str] = mapped_column(
        IdText(IdKind.APPLICATION),
        ForeignKey("application_pipeline.application_id"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
    evidence_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    meaningful_change: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[str] = mapped_column(UTCText(), nullable=False)


class ModelUsage(Base):
    """Architecture v2 `model_usage` row."""

    __tablename__ = "model_usage"
    __table_args__ = (CheckConstraint("success IN (0, 1)"),)

    model_usage_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.TASK), ForeignKey("agent_queue.task_id"), nullable=True
    )
    application_id: Mapped[str | None] = mapped_column(
        IdText(IdKind.APPLICATION), ForeignKey("application_pipeline.application_id"), nullable=True
    )
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_effort: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(REAL, nullable=True)
    response_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(UTCText(), nullable=False)
