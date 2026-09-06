"""Approval-fenced submission ledger and conservative unknown-result reconciliation."""

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import (
    ApplicationDetails,
    ApplicationPipeline,
    Approval,
    ExternalAction,
)
from job_hunting_machine.database.repositories import TaskCreate, TaskRepository
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.submission.review import (
    ReviewError,
    canonical_json,
    read_review,
    review_payload,
)
from job_hunting_machine.submission.types import (
    SubmissionAdapter,
    SubmissionOutcome,
    SubmissionResult,
)


class SubmissionError(RuntimeError):
    """Submission invariants failed without exposing application data."""


@dataclass(frozen=True, slots=True)
class SubmissionExecution:
    outcome: SubmissionOutcome
    action_id: str | None = None
    reason: str | None = None


class ExternalActionService:
    """The only final-submission effect boundary and its durable action ledger."""

    def __init__(
        self,
        queue: QueueService,
        adapter: SubmissionAdapter,
        *,
        runtime_mode: RuntimeMode,
        authorized_user_ids: frozenset[str],
    ) -> None:
        self.queue = queue
        self.adapter = adapter
        self.runtime_mode = runtime_mode
        self.authorized_user_ids = authorized_user_ids

    @staticmethod
    def _deadline(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _validated(self, lease: Lease) -> tuple[str, str, dict[str, object], str, str]:
        if self.runtime_mode is not RuntimeMode.LIVE:
            raise SubmissionError("submission_requires_live_mode")
        application_id = lease.payload.get("application_id")
        approval_id = lease.payload.get("approval_id")
        expected_path = lease.payload.get("review_path")
        expected_sha = lease.payload.get("review_sha256")
        correlated = (application_id, approval_id, expected_path, expected_sha)
        if not all(isinstance(item, str) for item in correlated):
            raise SubmissionError("submission_task_payload_invalid")
        assert isinstance(application_id, str)
        assert isinstance(approval_id, str)
        assert isinstance(expected_path, str)
        assert isinstance(expected_sha, str)
        validate_id(application_id, IdKind.APPLICATION)
        validate_id(approval_id, IdKind.APPROVAL)
        expired = False
        with self.queue.fence(lease) as session:
            task = self.queue._get(session, lease.task_id)
            app = session.get(ApplicationPipeline, application_id)
            details = session.get(ApplicationDetails, application_id)
            approval = session.get(Approval, approval_id)
            if (
                task.task_type != "SUBMIT_APPLICATION"
                or task.application_id != application_id
                or not app
                or not details
                or app.current_task_id != lease.task_id
                or not approval
                or approval.approval_type != "SUBMIT_APPLICATION"
                or approval.application_id != application_id
                or approval.task_id != lease.task_id
                or approval.payload_path != expected_path
                or approval.payload_sha256 != expected_sha
            ):
                raise SubmissionError("submission_correlation_invalid")
            if approval.approval_status == "APPROVED":
                if (
                    not approval.decided_by_slack_user_id
                    or approval.decided_by_slack_user_id not in self.authorized_user_ids
                ):
                    raise SubmissionError("submission_approver_not_authorized")
                if not approval.valid_until or self.queue.clock.now() >= self._deadline(
                    approval.valid_until
                ):
                    approval.approval_status = "EXPIRED"
                    ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                        ActivityEvent(
                            "submission_approval_expired",
                            task_id=lease.task_id,
                            application_id=application_id,
                            old_state="APPROVED",
                            new_state="EXPIRED",
                            metadata={"approval_id": approval_id},
                        )
                    )
                    expired = True
            elif approval.approval_status != "CONSUMED":
                raise SubmissionError(f"submission_approval_{approval.approval_status.lower()}")
            if app.application_status not in {
                "READY_TO_REVIEW",
                "SUBMISSION_APPROVED",
                "SUBMITTING",
                "SUBMISSION_UNKNOWN",
                "SUBMITTED",
            }:
                raise SubmissionError("submission_application_state_invalid")
        if expired:
            raise SubmissionError("submission_approval_expired")
        try:
            approved = read_review(expected_path, expected_sha)
        except ReviewError as error:
            self._revoke(lease, application_id, approval_id, "review_payload_changed")
            raise SubmissionError(str(error)) from None
        created_at = approved.get("created_at")
        if not isinstance(created_at, str):
            self._revoke(lease, application_id, approval_id, "review_payload_invalid")
            raise SubmissionError("review_created_at_invalid")
        try:
            with self.queue.database.transaction() as session:
                current = review_payload(session, application_id, created_at=created_at)
        except (ReviewError, ValueError):
            self._revoke(lease, application_id, approval_id, "approved_inputs_changed")
            raise SubmissionError("approved_inputs_changed") from None
        if canonical_json(current) != canonical_json(approved):
            self._revoke(lease, application_id, approval_id, "approved_inputs_changed")
            raise SubmissionError("approved_inputs_changed")
        return application_id, approval_id, approved, expected_sha, expected_path

    def _revoke(self, lease: Lease, application_id: str, approval_id: str, reason: str) -> None:
        with self.queue.fence(lease) as session:
            approval = session.get(Approval, approval_id)
            app = session.get(ApplicationPipeline, application_id)
            details = session.get(ApplicationDetails, application_id)
            if approval and approval.approval_status in {"PENDING", "APPROVED"}:
                approval.approval_status = "REVOKED"
                approval.rejection_reason = reason
            if app and details and app.application_status != "SUBMITTED":
                app.pipeline_stage = "REVIEW"
                app.application_status = details.application_status = "READY_TO_REVIEW"
                app.updated_at = details.updated_at = format_utc(self.queue.clock.now())
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "submission_approval_revoked",
                    application_id=application_id,
                    old_state="APPROVED",
                    new_state="REVOKED",
                    metadata={"approval_id": approval_id, "reason": reason},
                )
            )

    def _action(
        self,
        lease: Lease,
        application_id: str,
        approval_id: str,
        review_sha: str,
    ) -> tuple[str, str]:
        key = f"submit:{application_id}:{review_sha}"
        with self.queue.fence(lease) as session:
            existing = session.scalar(
                select(ExternalAction).where(ExternalAction.idempotency_key == key)
            )
            if existing:
                if (
                    existing.application_id != application_id
                    or existing.task_id != lease.task_id
                    or existing.approval_id != approval_id
                    or existing.action_type != "SUBMIT_APPLICATION"
                    or existing.request_sha256 != review_sha
                ):
                    raise SubmissionError("submission_idempotency_conflict")
                return existing.external_action_id, existing.action_status
            other = session.scalar(
                select(ExternalAction).where(
                    ExternalAction.application_id == application_id,
                    ExternalAction.action_type == "SUBMIT_APPLICATION",
                    ExternalAction.action_status.in_(["EXECUTING", "UNKNOWN_RESULT", "SUCCEEDED"]),
                )
            )
            if other:
                raise SubmissionError("submission_action_already_exists")
            now = format_utc(self.queue.clock.now())
            action_id = self.queue.ids.generate_ulid()
            session.add(
                ExternalAction(
                    external_action_id=action_id,
                    application_id=application_id,
                    task_id=lease.task_id,
                    approval_id=approval_id,
                    action_type="SUBMIT_APPLICATION",
                    idempotency_key=key,
                    request_sha256=review_sha,
                    action_status="PLANNED",
                    result_json=json.dumps({"review_sha256": review_sha}),
                    created_at=now,
                    updated_at=now,
                )
            )
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "submission_action_planned",
                    task_id=lease.task_id,
                    application_id=application_id,
                    new_state="PLANNED",
                    metadata={"external_action_id": action_id},
                )
            )
            return action_id, "PLANNED"

    def _start(
        self,
        lease: Lease,
        action_id: str,
        approval_id: str,
        application_id: str,
        review: dict[str, object],
        review_path: str,
        review_sha: str,
    ) -> None:
        with self.queue.fence(lease) as session:
            action = session.get(ExternalAction, action_id)
            approval = session.get(Approval, approval_id)
            app = session.get(ApplicationPipeline, application_id)
            details = session.get(ApplicationDetails, application_id)
            if (
                not action
                or action.action_status != "PLANNED"
                or not approval
                or approval.approval_status != "APPROVED"
                or not app
                or not details
                or app.application_status != "READY_TO_REVIEW"
            ):
                raise SubmissionError("submission_start_state_invalid")
            created_at = review.get("created_at")
            try:
                if not isinstance(created_at, str):
                    raise ReviewError("review_created_at_invalid")
                current_file = read_review(review_path, review_sha)
                current_data = review_payload(session, application_id, created_at=created_at)
                changed = canonical_json(current_file) != canonical_json(review) or canonical_json(
                    current_data
                ) != canonical_json(review)
            except (ReviewError, ValueError):
                changed = True
            if changed:
                approval.approval_status = "REVOKED"
                approval.rejection_reason = "approved_inputs_changed_before_click"
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "submission_approval_revoked",
                        task_id=lease.task_id,
                        application_id=application_id,
                        old_state="APPROVED",
                        new_state="REVOKED",
                        metadata={"approval_id": approval_id},
                    )
                )
            else:
                now = format_utc(self.queue.clock.now())
                approval.approval_status = "CONSUMED"
                approval.consumed_at = now
                app.pipeline_stage = "SUBMISSION"
                app.application_status = details.application_status = "SUBMISSION_APPROVED"
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "submission_approval_consumed",
                        task_id=lease.task_id,
                        application_id=application_id,
                        old_state="READY_TO_REVIEW",
                        new_state="SUBMISSION_APPROVED",
                        metadata={"external_action_id": action_id, "approval_id": approval_id},
                    )
                )
                app.application_status = details.application_status = "SUBMITTING"
                app.updated_at = details.updated_at = now
                action.action_status = "EXECUTING"
                action.updated_at = now
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "submission_started",
                        task_id=lease.task_id,
                        application_id=application_id,
                        old_state="SUBMISSION_APPROVED",
                        new_state="SUBMITTING",
                        metadata={"external_action_id": action_id, "approval_id": approval_id},
                    )
                )
        if changed:
            raise SubmissionError("approved_inputs_changed_before_click")

    def _record(
        self,
        lease: Lease,
        action_id: str,
        application_id: str,
        result: SubmissionResult,
        *,
        reconciled: bool,
    ) -> None:
        with self.queue.fence(lease) as session:
            action = session.get(ExternalAction, action_id)
            app = session.get(ApplicationPipeline, application_id)
            details = session.get(ApplicationDetails, application_id)
            assert action and app and details
            now = format_utc(self.queue.clock.now())
            if result.outcome is SubmissionOutcome.CONFIRMED:
                action.action_status = "SUCCEEDED"
                app.pipeline_stage = "POST_SUBMISSION"
                app.application_status = details.application_status = "SUBMITTED"
                details.submitted_at = details.submitted_at or now
                connector = TaskRepository(session, self.queue.clock, self.queue.ids).create(
                    TaskCreate(
                        "CONNECT_CONTACTS",
                        task_status="READY",
                        application_id=application_id,
                        job_id=app.job_id,
                        parent_task_id=lease.task_id,
                        dedupe_key=f"connect_contacts:{application_id}",
                        payload={
                            "application_id": application_id,
                            "source_urls": [details.job_url],
                        },
                    )
                )
                TaskRepository(session, self.queue.clock, self.queue.ids).create(
                    TaskCreate(
                        "MONITOR_APPLICATION",
                        task_status="READY",
                        application_id=application_id,
                        job_id=app.job_id,
                        parent_task_id=lease.task_id,
                        dedupe_key=f"monitor:{application_id}:initial",
                        payload={"application_id": application_id},
                    )
                )
                app.current_task_id = connector.task_id
            elif result.outcome is SubmissionOutcome.UNKNOWN:
                action.action_status = "UNKNOWN_RESULT"
                app.pipeline_stage = "SUBMISSION"
                app.application_status = details.application_status = "SUBMISSION_UNKNOWN"
            else:
                action.action_status = "FAILED"
                app.pipeline_stage = "REVIEW"
                app.application_status = details.application_status = "READY_TO_REVIEW"
            action.external_reference = result.external_reference
            action.result_json = json.dumps(
                {"outcome": result.outcome.value, "reconciled": reconciled}
            )
            action.updated_at = app.updated_at = details.updated_at = now
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "submission_reconciled" if reconciled else "submission_finished",
                    task_id=action.task_id,
                    application_id=application_id,
                    new_state=app.application_status,
                    metadata={"external_action_id": action_id},
                )
            )

    async def execute(self, lease: Lease) -> SubmissionExecution:
        application_id, approval_id, review, review_sha, review_path = self._validated(lease)
        action_id, status = self._action(lease, application_id, approval_id, review_sha)
        if status == "SUCCEEDED":
            return SubmissionExecution(SubmissionOutcome.CONFIRMED, action_id)
        if status in {"EXECUTING", "UNKNOWN_RESULT"}:
            result = await self.adapter.reconcile(application_id, review)
            self._record(lease, action_id, application_id, result, reconciled=True)
            return SubmissionExecution(result.outcome, action_id)
        self._start(
            lease,
            action_id,
            approval_id,
            application_id,
            review,
            review_path,
            review_sha,
        )
        try:
            result = await self.adapter.submit(application_id, review)
        except Exception:
            result = SubmissionResult(SubmissionOutcome.UNKNOWN)
        self._record(lease, action_id, application_id, result, reconciled=False)
        return SubmissionExecution(result.outcome, action_id)

    def recover(self) -> int:
        """Never infer success after a crash; require read-only reconciliation."""
        with self.queue.database.transaction(immediate=True) as session:
            rows = list(
                session.scalars(
                    select(ExternalAction).where(
                        ExternalAction.action_type == "SUBMIT_APPLICATION",
                        ExternalAction.action_status == "EXECUTING",
                    )
                )
            )
            now = format_utc(self.queue.clock.now())
            for row in rows:
                row.action_status = "UNKNOWN_RESULT"
                row.updated_at = now
                if row.application_id:
                    app = session.get(ApplicationPipeline, row.application_id)
                    details = session.get(ApplicationDetails, row.application_id)
                    if app and details:
                        app.application_status = details.application_status = "SUBMISSION_UNKNOWN"
                        app.updated_at = details.updated_at = now
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "submission_execution_recovered",
                        task_id=row.task_id,
                        application_id=row.application_id,
                        old_state="EXECUTING",
                        new_state="UNKNOWN_RESULT",
                        metadata={"external_action_id": row.external_action_id},
                    )
                )
            return len(rows)

    def schedule_new_review(self, lease: Lease, application_id: str) -> str:
        """Return a confirmed-not-submitted application to a fresh immutable review."""
        with self.queue.fence(lease) as session:
            app = session.get(ApplicationPipeline, application_id)
            if not app or app.application_status != "READY_TO_REVIEW":
                raise SubmissionError("review_restart_state_invalid")
            token = self.queue.ids.generate_ulid()
            task = TaskRepository(session, self.queue.clock, self.queue.ids).create(
                TaskCreate(
                    "CREATE_REVIEW",
                    task_status="READY",
                    application_id=application_id,
                    job_id=app.job_id,
                    parent_task_id=lease.task_id,
                    dedupe_key=f"create_review:{application_id}:{token}",
                    payload={"application_id": application_id},
                )
            )
            app.current_task_id = task.task_id
            app.updated_at = format_utc(self.queue.clock.now())
            return task.task_id
