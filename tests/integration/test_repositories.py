"""Durable IDs, atomic application creation, dedupe, concurrency, and audit acceptance."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    ActivityLog,
    AgentTask,
    ApplicationDetails,
    ApplicationPipeline,
    Job,
)
from job_hunting_machine.database.repositories import (
    ActivityEvent,
    ActivityLogRepository,
    ApplicationCreate,
    ApplicationRepository,
    ApprovalCreate,
    ApprovalRepository,
    ArtifactCreate,
    ArtifactRepository,
    ConcurrentUpdateError,
    JobCreate,
    JobRepository,
    ReplayConflictError,
    TaskCreate,
    TaskRepository,
)
from job_hunting_machine.ids import IdGenerator, IdKind, validate_id

_CLOCK = FrozenClock(datetime(2026, 9, 5, 12, 30, tzinfo=UTC))
_STAMP = "2026-09-05T12:30:00.000Z"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "repositories.db")
    database.migrate()
    try:
        yield database
    finally:
        database.dispose()


def _job(session: Session, *, status: str = "PASSED") -> Job:
    return JobRepository(session, _CLOCK).create(
        JobCreate(
            original_url="https://example.invalid/jobs/1?source=test",
            canonical_url="https://example.invalid/jobs/1",
            source_type="TEST_FIXTURE",
            company_name="Synthetic Company",
            job_title="Synthetic Role",
            qualification_status=status,
        )
    )


def _details(job: Job) -> ApplicationCreate:
    return ApplicationCreate("Synthetic Company", "Synthetic Role", job.canonical_url)


def test_duplicate_url_returns_original_job_and_does_not_duplicate_audit(
    database: Database,
) -> None:
    with database.transaction() as session:
        first = _job(session)
        repeated = _job(session)
        assert repeated.job_id == first.job_id
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        assert session.scalar(select(func.count()).select_from(ActivityLog)) == 1
        assert first.first_seen_at == _STAMP


def test_unique_url_constraint_protects_raw_duplicate_writes(database: Database) -> None:
    with database.transaction() as session:
        original = _job(session)
        url = original.canonical_url
    with pytest.raises(IntegrityError), database.transaction() as session:
        session.add(
            Job(
                job_id=IdGenerator().new(IdKind.JOB),
                original_url=url,
                canonical_url=url,
                url_sha256="0" * 64,
                source_type="TEST",
                qualification_status="NEW",
                first_seen_at=_STAMP,
                updated_at=_STAMP,
            )
        )


def test_dedupe_reuses_persisted_task_id_and_state(database: Database) -> None:
    data = TaskCreate("TEST_TASK", dedupe_key="test:one", payload={"x": 1})
    with database.transaction() as session:
        repository = TaskRepository(session, _CLOCK)
        original = repository.create(data)
        updated = repository.update_status(original.task_id, "READY", expected_version=1)
        repeated = repository.create(data)
        assert repeated.task_id == original.task_id
        assert repeated.task_status == "READY"
        assert repeated.version == updated.version == 2
        assert len(ActivityLogRepository(session).list_for_task(original.task_id)) == 2


def test_duplicate_dedupe_key_is_enforced_by_sqlite(database: Database) -> None:
    with database.transaction() as session:
        TaskRepository(session).create(TaskCreate("TEST_TASK", dedupe_key="unique"))
    with pytest.raises(IntegrityError), database.transaction() as session:
        session.add(
            AgentTask(
                task_id=IdGenerator().task_id(),
                task_type="OTHER",
                task_status="NEW",
                dedupe_key="unique",
                created_at=_STAMP,
                updated_at=_STAMP,
            )
        )


@pytest.mark.parametrize("change", ["payload", "dedupe_key"])
def test_task_replay_cannot_reuse_identity_for_different_work(
    database: Database, change: str
) -> None:
    with database.transaction() as session:
        repo = TaskRepository(session)
        original = TaskCreate("TEST_TASK", dedupe_key="one", payload={"a": 1})
        task = repo.create(original)
        changed = (
            replace(original, payload={"a": 2})
            if change == "payload"
            else replace(original, task_id=task.task_id, dedupe_key="two")
        )
        with pytest.raises(ReplayConflictError):
            repo.create(changed)


def test_passed_job_atomically_creates_application_details_resume_task_and_events(
    database: Database,
) -> None:
    with database.transaction() as session:
        job = _job(session)
        result = ApplicationRepository(session, _CLOCK).create_from_passed_job(
            job.job_id, _details(job)
        )
        assert result.created
        assert (
            validate_id(result.application_id, IdKind.APPLICATION) == result.details.application_id
        )
        assert validate_id(result.task_id, IdKind.TASK) == result.pipeline.current_task_id
        assert result.task.application_id == result.application_id
        assert result.task.job_id == job.job_id
        assert result.task.task_type == "BUILD_RESUME"
        assert result.task.task_status == "READY"
        assert result.task.dedupe_key == f"build_resume:{result.application_id}"
        assert (
            result.pipeline.application_status == result.details.application_status == "QUALIFIED"
        )
        assert result.pipeline.created_at == _STAMP
        events = ActivityLogRepository(session).list_for_application(result.application_id)
        assert {event.event_type for event in events} == {"task_created", "application_created"}
        for model in (ApplicationPipeline, ApplicationDetails, AgentTask):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_application_retry_after_restart_reuses_exact_ids(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with database.transaction() as session:
        job = _job(session)
        job_id, data = job.job_id, _details(job)
        original = ApplicationRepository(session).create_from_passed_job(job_id, data)
        original_ids = original.application_id, original.task_id
    database.dispose()
    reopened = Database(database.path)

    def forbidden_generation(self: IdGenerator, kind: IdKind) -> str:
        raise AssertionError("Replay must not generate a new identifier")

    monkeypatch.setattr(IdGenerator, "new", forbidden_generation)
    try:
        with reopened.transaction() as session:
            repeated = ApplicationRepository(session).create_from_passed_job(job_id, data)
            assert not repeated.created
            assert (repeated.application_id, repeated.task_id) == original_ids
            assert session.scalar(select(func.count()).select_from(ActivityLog)) == 3
    finally:
        reopened.dispose()


def test_failed_creation_savepoint_has_no_partial_application_even_if_error_caught(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_append = ActivityLogRepository.append

    def fail_last_audit(self: ActivityLogRepository, event: ActivityEvent) -> ActivityLog:
        if event.event_type == "application_created":
            raise RuntimeError("injected after pipeline, details, task and task audit")
        return original_append(self, event)

    monkeypatch.setattr(ActivityLogRepository, "append", fail_last_audit)
    with database.transaction() as session:
        job = _job(session)
        with pytest.raises(RuntimeError, match="injected"):
            ApplicationRepository(session).create_from_passed_job(job.job_id, _details(job))
        for model in (ApplicationPipeline, ApplicationDetails, AgentTask):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalar(select(func.count()).select_from(ActivityLog)) == 1
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ApplicationPipeline)) == 0


def test_outer_transaction_failure_undoes_released_repository_savepoints(
    database: Database,
) -> None:
    with pytest.raises(RuntimeError), database.transaction() as session:
        job = _job(session)
        ApplicationRepository(session).create_from_passed_job(job.job_id, _details(job))
        raise RuntimeError("injected before commit")
    with database.transaction() as session:
        for model in (Job, ApplicationPipeline, ApplicationDetails, AgentTask, ActivityLog):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("status", ["NEW", "ACTIVE", "ABORTED", "NEEDS_REVIEW", "ERROR"])
def test_application_requires_passed_job(database: Database, status: str) -> None:
    with database.transaction() as session:
        job = _job(session, status=status)
        with pytest.raises(ValueError, match="PASSED"):
            ApplicationRepository(session).create_from_passed_job(job.job_id, _details(job))


def test_application_cannot_bind_another_job_url(database: Database) -> None:
    with database.transaction() as session:
        job = _job(session)
        with pytest.raises(ValueError, match="job URL"):
            ApplicationRepository(session).create_from_passed_job(
                job.job_id, replace(_details(job), job_url="https://example.invalid/jobs/other")
            )


def test_stale_task_version_cannot_overwrite_or_add_an_audit(database: Database) -> None:
    with database.transaction() as session:
        task_id = TaskRepository(session).create(TaskCreate("TEST_TASK")).task_id
    with database.transaction() as session:
        changed = TaskRepository(session).update_status(task_id, "READY", expected_version=1)
        assert changed.version == 2
    with database.transaction() as session:
        repo = TaskRepository(session)
        with pytest.raises(ConcurrentUpdateError):
            repo.update_status(task_id, "CANCELED", expected_version=1)
        persisted = repo.get(task_id)
        assert persisted is not None and persisted.version == 2 and persisted.task_status == "READY"
        events = ActivityLogRepository(session).list_for_task(task_id)
        assert len(events) == 2
        transition = next(event for event in events if event.event_type == "task_status_changed")
        assert (transition.old_state, transition.new_state) == ("NEW", "READY")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE activity_log SET event_type='changed' WHERE event_id=:id",
        "DELETE FROM activity_log WHERE event_id=:id",
        "INSERT OR REPLACE INTO activity_log(event_id,actor_type,event_type,created_at) "
        "VALUES (:id,'SYSTEM','changed','2026-09-05T00:00:00.000Z')",
    ],
)
def test_audit_history_cannot_be_updated_deleted_or_replaced(database: Database, sql: str) -> None:
    with database.transaction() as session:
        event_id = ActivityLogRepository(session).append(ActivityEvent("original")).event_id
    with pytest.raises(IntegrityError, match="append-only"), database.transaction() as session:
        session.execute(text(sql), {"id": event_id})
    with database.transaction() as session:
        event = session.get(ActivityLog, event_id)
        assert event is not None and event.event_type == "original"
        assert not hasattr(ActivityLogRepository(session), "update")
        assert not hasattr(ActivityLogRepository(session), "delete")


def test_artifact_and_approval_ids_persist_and_replay_exact_metadata(database: Database) -> None:
    with database.transaction() as session:
        artifact_data = ArtifactCreate("OTHER", ".tmp/metadata-only.txt", "a" * 64, "text/plain")
        artifact = ArtifactRepository(session).create(artifact_data)
        approval_data = ApprovalCreate("SUBMIT_APPLICATION", ".tmp/review.json", "b" * 64)
        approval = ApprovalRepository(session).create(approval_data)
        artifact_id, approval_id = artifact.artifact_id, approval.approval_id
        validate_id(artifact_id, IdKind.ARTIFACT)
        validate_id(approval_id, IdKind.APPROVAL)
    with database.transaction() as session:
        repeated_artifact = ArtifactRepository(session).create(
            replace(artifact_data, artifact_id=artifact_id)
        )
        repeated_approval = ApprovalRepository(session).create(
            replace(approval_data, approval_id=approval_id)
        )
        assert repeated_artifact.artifact_id == artifact_id
        assert repeated_approval.approval_id == approval_id
        assert repeated_approval.approval_status == "PENDING"
        assert not repeated_artifact.approved_for_submission
        with pytest.raises(ReplayConflictError):
            ArtifactRepository(session).create(
                replace(artifact_data, artifact_id=artifact_id, sha256="c" * 64)
            )
        with pytest.raises(ReplayConflictError):
            ApprovalRepository(session).create(
                replace(approval_data, approval_id=approval_id, payload_sha256="c" * 64)
            )
        assert session.scalar(select(func.count()).select_from(ActivityLog)) == 2


def test_qualification_status_storage_is_audited_without_evaluating_rules(
    database: Database,
) -> None:
    with database.transaction() as session:
        job = _job(session, status="NEW")
        result = JobRepository(session).set_qualification_status(
            job.job_id, "PASSED", expected_status="NEW"
        )
        assert result.qualification_status == "PASSED"
        transition = session.scalar(
            select(ActivityLog).where(ActivityLog.event_type == "job_qualification_status_changed")
        )
        assert (
            transition is not None
            and transition.old_state == "NEW"
            and transition.new_state == "PASSED"
        )
