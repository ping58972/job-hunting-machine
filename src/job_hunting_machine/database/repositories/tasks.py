"""Durable tasks and versioned updates; no claiming, leasing or execution."""

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from job_hunting_machine.database.models import AgentTask
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import (
    ConcurrentUpdateError,
    RecordNotFoundError,
    ReplayConflictError,
    Repository,
    canonical_json,
    required_text,
)
from job_hunting_machine.ids import IdKind, validate_id

TASK_STATUSES = frozenset(
    {
        "NEW",
        "READY",
        "ACTIVE",
        "WAITING_HUMAN",
        "WAITING_RETRY",
        "SUCCEEDED",
        "ABORTED",
        "FAILED",
        "CANCELED",
    }
)
_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "ABORTED", "FAILED", "CANCELED"})


@dataclass(frozen=True, slots=True)
class TaskCreate:
    task_type: str
    dedupe_key: str | None = None
    application_id: str | None = None
    job_id: str | None = None
    parent_task_id: str | None = None
    payload: Mapping[str, object] | None = None
    task_status: str = "NEW"
    priority: int = 100
    max_attempts: int = 3
    task_id: str | None = None


class TaskRepository(Repository):
    def get(self, task_id: str) -> AgentTask | None:
        return self.session.get(AgentTask, validate_id(task_id, IdKind.TASK))

    def get_by_dedupe_key(self, dedupe_key: str) -> AgentTask | None:
        return self.session.scalar(select(AgentTask).where(AgentTask.dedupe_key == dedupe_key))

    @staticmethod
    def _check_replay(existing: AgentTask, data: TaskCreate, payload_json: str | None) -> AgentTask:
        if (
            existing.task_type != data.task_type
            or existing.dedupe_key != data.dedupe_key
            or existing.application_id != data.application_id
            or existing.job_id != data.job_id
            or existing.parent_task_id != data.parent_task_id
            or existing.payload_json != payload_json
            or (data.task_id is not None and existing.task_id != data.task_id)
        ):
            raise ReplayConflictError("Task identity was reused for a different operation")
        return existing

    def create(self, data: TaskCreate) -> AgentTask:
        required_text(data.task_type)
        if data.dedupe_key is not None:
            required_text(data.dedupe_key)
        if data.task_status not in TASK_STATUSES:
            raise ValueError("Invalid task status")
        if data.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        for value, kind in (
            (data.task_id, IdKind.TASK),
            (data.parent_task_id, IdKind.TASK),
            (data.application_id, IdKind.APPLICATION),
            (data.job_id, IdKind.JOB),
        ):
            if value is not None:
                validate_id(value, kind)
        payload_json = canonical_json(data.payload)
        existing = self.get(data.task_id) if data.task_id is not None else None
        if existing is None and data.dedupe_key is not None:
            existing = self.get_by_dedupe_key(data.dedupe_key)
        if existing is not None:
            return self._check_replay(existing, data, payload_json)
        try:
            with self.session.begin_nested():
                now = self.timestamp()
                record = AgentTask(
                    task_id=data.task_id or self.ids.new(IdKind.TASK),
                    task_type=data.task_type,
                    task_status=data.task_status,
                    application_id=data.application_id,
                    job_id=data.job_id,
                    parent_task_id=data.parent_task_id,
                    payload_json=payload_json,
                    dedupe_key=data.dedupe_key,
                    priority=data.priority,
                    max_attempts=data.max_attempts,
                    attempt_count=0,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(record)
                self.session.flush()
                ActivityLogRepository(self.session, self.clock, self.ids).append(
                    ActivityEvent(
                        "task_created",
                        task_id=record.task_id,
                        application_id=record.application_id,
                        job_id=record.job_id,
                        new_state=record.task_status,
                    )
                )
            return record
        except IntegrityError:
            existing = self.get(data.task_id) if data.task_id is not None else None
            if existing is None and data.dedupe_key is not None:
                existing = self.get_by_dedupe_key(data.dedupe_key)
            if existing is None:
                raise
            return self._check_replay(existing, data, payload_json)

    def update_status(self, task_id: str, status: str, *, expected_version: int) -> AgentTask:
        """Compare-and-swap a stored status and append its audit atomically.

        This is a persistence primitive, not the Phase 2 queue state machine.
        Caller policy is responsible for authorizing a particular transition.
        """
        validate_id(task_id, IdKind.TASK)
        if status not in TASK_STATUSES:
            raise ValueError("Invalid task status")
        if expected_version < 1:
            raise ValueError("expected_version must be positive")
        with self.session.begin_nested():
            record = self.session.scalar(
                select(AgentTask)
                .where(AgentTask.task_id == task_id)
                .execution_options(populate_existing=True)
            )
            if record is None:
                raise RecordNotFoundError("Task does not exist")
            if record.version != expected_version:
                raise ConcurrentUpdateError("Task version changed")
            previous_status = record.task_status
            now = self.timestamp()
            changed_id = self.session.scalar(
                update(AgentTask)
                .where(AgentTask.task_id == task_id, AgentTask.version == expected_version)
                .values(
                    task_status=status,
                    version=expected_version + 1,
                    updated_at=now,
                    completed_at=now if status in _TERMINAL_STATUSES else None,
                )
                .returning(AgentTask.task_id)
                .execution_options(synchronize_session=False)
            )
            if changed_id is None:
                raise ConcurrentUpdateError("Task version changed")
            ActivityLogRepository(self.session, self.clock, self.ids).append(
                ActivityEvent(
                    "task_status_changed",
                    task_id=task_id,
                    application_id=record.application_id,
                    job_id=record.job_id,
                    old_state=previous_status,
                    new_state=status,
                    metadata={
                        "previous_version": expected_version,
                        "version": expected_version + 1,
                    },
                )
            )
            self.session.refresh(record)
        return record
