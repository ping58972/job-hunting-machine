"""Idempotent Monitor Event persistence and audited state transitions."""

from dataclasses import dataclass

from sqlalchemy import select

from job_hunting_machine.database.models import (
    ApplicationDetails,
    ApplicationPipeline,
    MonitorEvent,
)
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import Repository, required_text
from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.monitor.types import (
    DESTRUCTIVE_STATUSES,
    TERMINAL_STATUSES,
    StatusClassification,
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "SUBMITTED": frozenset({"UNDER_REVIEW", "REJECTED"}),
    "UNDER_REVIEW": frozenset({"ASSESSMENT", "RECRUITER_SCREEN", "INTERVIEW", "REJECTED"}),
    "ASSESSMENT": frozenset({"INTERVIEW", "REJECTED"}),
    "RECRUITER_SCREEN": frozenset({"INTERVIEW", "REJECTED"}),
    "INTERVIEW": frozenset({"FINAL_INTERVIEW", "OFFER", "REJECTED"}),
    "FINAL_INTERVIEW": frozenset({"OFFER", "REJECTED"}),
    "OFFER": frozenset({"CLOSED"}),
    "REJECTED": frozenset({"CLOSED"}),
    "WITHDRAWN": frozenset({"CLOSED"}),
    "CANCELED": frozenset({"CLOSED"}),
}


@dataclass(frozen=True, slots=True)
class MonitorResult:
    event_id: str | None
    changed: bool
    duplicate: bool
    previous_status: str | None
    detected_status: str | None


class MonitorEventRepository(Repository):
    def get_by_source(self, source_type: str, source_reference: str) -> MonitorEvent | None:
        return self.session.scalar(
            select(MonitorEvent).where(
                MonitorEvent.source_type == source_type,
                MonitorEvent.source_reference == source_reference,
            )
        )

    def record(
        self,
        *,
        task_id: str,
        application_id: str,
        source_type: str,
        source_reference: str,
        evidence_path: str,
        classification: StatusClassification,
        transition_confidence: float,
        destructive_confidence: float,
    ) -> MonitorResult:
        validate_id(task_id, IdKind.TASK)
        validate_id(application_id, IdKind.APPLICATION)
        if source_type not in {"EMAIL", "PORTAL"}:
            raise ValueError("monitor_source_invalid")
        required_text(source_reference)
        existing = self.get_by_source(source_type, source_reference)
        if existing:
            return MonitorResult(
                existing.monitor_event_id,
                False,
                True,
                existing.previous_status,
                existing.detected_status,
            )
        app = self.session.get(ApplicationPipeline, application_id)
        details = self.session.get(ApplicationDetails, application_id)
        if app is None or details is None:
            raise ValueError("monitor_application_missing")
        previous = app.application_status
        detected = (
            classification.detected_status.value
            if classification.detected_status is not None
            else None
        )
        threshold = (
            destructive_confidence if detected in DESTRUCTIVE_STATUSES else transition_confidence
        )
        changed = bool(
            detected
            and detected != previous
            and previous not in TERMINAL_STATUSES
            and detected in ALLOWED_TRANSITIONS.get(previous, frozenset())
            and classification.confidence >= threshold
        )
        now = self.timestamp()
        event_id = self.ids.generate_ulid()
        self.session.add(
            MonitorEvent(
                monitor_event_id=event_id,
                application_id=application_id,
                source_type=source_type,
                source_reference=source_reference,
                detected_status=detected,
                previous_status=previous,
                confidence=classification.confidence,
                evidence_path=evidence_path,
                meaningful_change=int(changed),
                observed_at=now,
            )
        )
        details.last_status_checked_at = now
        details.updated_at = now
        ActivityLogRepository(self.session, self.clock, self.ids).append(
            ActivityEvent(
                "monitor_observation_recorded",
                task_id=task_id,
                application_id=application_id,
                old_state=previous,
                new_state=detected,
                metadata={
                    "monitor_event_id": event_id,
                    "source_type": source_type,
                    "meaningful_change": changed,
                },
            )
        )
        if changed:
            app.application_status = detected or previous
            details.application_status = detected or previous
            app.pipeline_stage = "CLOSED" if detected in TERMINAL_STATUSES else "MONITORING"
            app.updated_at = now
            if detected in TERMINAL_STATUSES:
                app.closed_at = now
            ActivityLogRepository(self.session, self.clock, self.ids).append(
                ActivityEvent(
                    "application_status_changed",
                    task_id=task_id,
                    application_id=application_id,
                    old_state=previous,
                    new_state=detected,
                    metadata={"monitor_event_id": event_id, "source_type": source_type},
                )
            )
        self.session.flush()
        return MonitorResult(event_id, changed, False, previous, detected)
