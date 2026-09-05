"""Append/read audit API. Historical records have no update/delete operation."""

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select

from job_hunting_machine.database.models import ActivityLog
from job_hunting_machine.database.repositories.base import (
    Repository,
    canonical_json,
    required_text,
)
from job_hunting_machine.ids import IdKind, validate_id


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    event_type: str
    task_id: str | None = None
    application_id: str | None = None
    job_id: str | None = None
    actor_type: str = "SYSTEM"
    actor_name: str | None = None
    old_state: str | None = None
    new_state: str | None = None
    metadata: Mapping[str, object] | None = None


class ActivityLogRepository(Repository):
    def append(self, event: ActivityEvent) -> ActivityLog:
        """Append safe audit context in the same transaction as its mutation."""
        required_text(event.event_type)
        for value, kind in (
            (event.task_id, IdKind.TASK),
            (event.application_id, IdKind.APPLICATION),
            (event.job_id, IdKind.JOB),
        ):
            if value is not None:
                validate_id(value, kind)
        if event.actor_type not in {"SYSTEM", "AGENT", "USER", "MODEL", "SLACK", "BROWSER"}:
            raise ValueError("Invalid audit actor type")
        record = ActivityLog(
            event_id=self.ids.new(IdKind.EVENT),
            task_id=event.task_id,
            application_id=event.application_id,
            job_id=event.job_id,
            actor_type=event.actor_type,
            actor_name=event.actor_name,
            event_type=event.event_type,
            old_state=event.old_state,
            new_state=event.new_state,
            metadata_json=canonical_json(event.metadata),
            created_at=self.timestamp(),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def list_for_task(self, task_id: str) -> list[ActivityLog]:
        validate_id(task_id, IdKind.TASK)
        return list(
            self.session.scalars(
                select(ActivityLog)
                .where(ActivityLog.task_id == task_id)
                .order_by(ActivityLog.created_at, ActivityLog.event_id)
            )
        )

    def list_for_application(self, application_id: str) -> list[ActivityLog]:
        validate_id(application_id, IdKind.APPLICATION)
        return list(
            self.session.scalars(
                select(ActivityLog)
                .where(ActivityLog.application_id == application_id)
                .order_by(ActivityLog.created_at, ActivityLog.event_id)
            )
        )
