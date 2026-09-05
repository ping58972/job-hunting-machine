"""Record hash-bound Slack decisions only. This service cannot submit or send outreach."""

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_hunting_machine.clock import Clock, format_utc
from job_hunting_machine.database.models import (
    AgentTask,
    ApplicationPipeline,
    Approval,
    ExternalAction,
)
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.ids import IdGenerator
from job_hunting_machine.security.paths import PathGuard
from job_hunting_machine.slack.config import SlackInputError, SlackSettings


class ApprovalService:
    def __init__(self, settings: SlackSettings, clock: Clock) -> None:
        self.settings, self.clock = settings, clock
        self.ids = IdGenerator(clock)

    def verify(self, session: Session, approval: Approval) -> None:
        task = session.get(AgentTask, approval.task_id) if approval.task_id else None
        app = (
            session.get(ApplicationPipeline, approval.application_id)
            if approval.application_id
            else None
        )
        if not task or not app or task.application_id != app.application_id:
            raise SlackInputError("approval_correlation_invalid")
        if approval.approval_type == "SUBMIT_APPLICATION":
            if app.application_status != "READY_TO_REVIEW":
                raise SlackInputError("application_not_ready_for_review")
            active = session.scalar(
                select(ExternalAction).where(
                    ExternalAction.application_id == app.application_id,
                    ExternalAction.action_type == "SUBMIT_APPLICATION",
                    ExternalAction.action_status.in_(["EXECUTING", "UNKNOWN_RESULT", "SUCCEEDED"]),
                )
            )
            if active:
                raise SlackInputError("submission_action_already_exists")
        requested = datetime.fromisoformat(approval.requested_at.replace("Z", "+00:00"))
        deadline = format_utc(requested + timedelta(seconds=self.settings.approval_ttl_seconds))
        if approval.valid_until:
            deadline = min(deadline, approval.valid_until)
        if format_utc(self.clock.now()) >= deadline:
            raise SlackInputError("approval_expired")
        try:
            path = PathGuard().validate_write(approval.payload_path)
            if path.stat().st_size > 5_000_000:
                raise SlackInputError("approval_payload_invalid")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except (ValueError, OSError):
            raise SlackInputError("approval_payload_unavailable") from None
        if actual != approval.payload_sha256:
            raise SlackInputError("approval_payload_changed")

    def decide(self, session: Session, approval: Approval, *, decision: str, user_id: str) -> None:
        if user_id not in self.settings.authorized_user_ids:
            raise SlackInputError("unauthorized_slack_user")
        if decision not in {"APPROVED", "REJECTED"}:
            raise SlackInputError("invalid_approval_decision")
        if approval.approval_status == decision and approval.decided_by_slack_user_id == user_id:
            return
        if approval.approval_status != "PENDING":
            raise SlackInputError("approval_already_decided")
        self.verify(session, approval)
        approval.approval_status = decision
        approval.decided_by_slack_user_id = user_id
        approval.decided_at = format_utc(self.clock.now())
        if decision == "REJECTED":
            approval.rejection_reason = "Rejected through authorized Slack control"
        ActivityLogRepository(session, self.clock, self.ids).append(
            ActivityEvent(
                "slack_approval_decided",
                task_id=approval.task_id,
                application_id=approval.application_id,
                actor_type="USER",
                actor_name="slack_authorized_user",
                old_state="PENDING",
                new_state=decision,
                metadata={"approval_id": approval.approval_id, "user_id": user_id},
            )
        )
