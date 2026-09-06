"""Lease-fenced Gmail draft creation and approval-bound email sending."""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import (
    Approval,
    ExternalAction,
    OutreachDraft,
)
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.outreach.email import (
    approval_value,
    canonical_email,
    email_payload,
    email_sha256,
)
from job_hunting_machine.outreach.types import EmailOutcome, EmailResult, GmailAdapter
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.submission.review import read_review


class OutreachActionError(RuntimeError):
    """An outreach external action failed a static safety invariant."""


class ConnectorExternalActionService:
    """Capability-restricted facade: Gmail draft creation only, with no send method."""

    def __init__(self, service: "ExternalActionService") -> None:
        self.__service = service

    async def create_draft(
        self, lease: Lease, outreach_id: str, attachment_ids: tuple[str, ...]
    ) -> str:
        return await self.__service.create_draft(lease, outreach_id, attachment_ids)

    def recover(self) -> int:
        return self.__service.recover(action_types=("GMAIL_CREATE_DRAFT",))


class ExternalActionService:
    """The only Gmail mutation boundary used by Connector and Outreach workers."""

    def __init__(
        self,
        queue: QueueService,
        adapter: GmailAdapter,
        *,
        runtime_mode: RuntimeMode,
        authorized_user_ids: frozenset[str],
    ) -> None:
        self.queue = queue
        self.adapter = adapter
        self.runtime_mode = runtime_mode
        self.authorized_user_ids = authorized_user_ids

    async def create_draft(
        self, lease: Lease, outreach_id: str, attachment_ids: tuple[str, ...]
    ) -> str:
        if self.runtime_mode is RuntimeMode.DRY_RUN:
            raise OutreachActionError("gmail_draft_disabled_in_dry_run")
        with self.queue.fence(lease) as session:
            task = self.queue._get(session, lease.task_id)
            draft = session.get(OutreachDraft, outreach_id)
            if (
                task.task_type != "CONNECT_CONTACTS"
                or not draft
                or draft.application_id != task.application_id
            ):
                raise OutreachActionError("gmail_draft_task_mismatch")
            payload = email_payload(session, outreach_id, attachment_ids)
            digest = email_sha256(payload)
            key = f"gmail-draft:{outreach_id}:{digest}"
            existing = session.scalar(
                select(ExternalAction).where(ExternalAction.idempotency_key == key)
            )
            if existing:
                if existing.action_status == "SUCCEEDED" and existing.external_reference:
                    return existing.external_reference
                raise OutreachActionError("gmail_draft_result_not_repeatable")
            now = format_utc(self.queue.clock.now())
            action_id = self.queue.ids.generate_ulid()
            session.add(
                ExternalAction(
                    external_action_id=action_id,
                    application_id=draft.application_id,
                    task_id=lease.task_id,
                    action_type="GMAIL_CREATE_DRAFT",
                    idempotency_key=key,
                    request_sha256=digest,
                    action_status="EXECUTING",
                    result_json=json.dumps({"outreach_id": outreach_id}),
                    created_at=now,
                    updated_at=now,
                )
            )
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "gmail_draft_started",
                    task_id=lease.task_id,
                    application_id=draft.application_id,
                    new_state="EXECUTING",
                    metadata={"external_action_id": action_id},
                )
            )
        try:
            result = await self.adapter.create_draft(payload, key)
        except Exception:
            result = EmailResult(EmailOutcome.UNKNOWN)
        with self.queue.fence(lease) as session:
            action = session.get(ExternalAction, action_id)
            assert action
            action.action_status = (
                "SUCCEEDED" if result.outcome is EmailOutcome.CONFIRMED else "UNKNOWN_RESULT"
            )
            action.external_reference = result.provider_id
            action.updated_at = format_utc(self.queue.clock.now())
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "gmail_draft_finished",
                    task_id=lease.task_id,
                    application_id=action.application_id,
                    new_state=action.action_status,
                    metadata={"external_action_id": action_id},
                )
            )
        if result.outcome is not EmailOutcome.CONFIRMED or not result.provider_id:
            raise OutreachActionError("gmail_draft_result_unknown")
        return result.provider_id

    def _revoke(
        self, session: Session, lease: Lease, approval: Approval, draft: OutreachDraft
    ) -> None:
        revoked = approval.approval_status == "APPROVED"
        if revoked:
            approval.approval_status = "REVOKED"
            approval.rejection_reason = "email_payload_changed"
        draft.draft_status = "READY_FOR_REVIEW"
        draft.updated_at = format_utc(self.queue.clock.now())
        if revoked:
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "email_approval_revoked",
                    task_id=lease.task_id,
                    application_id=draft.application_id,
                    old_state="APPROVED",
                    new_state="REVOKED",
                    metadata={"approval_id": approval.approval_id},
                )
            )

    async def send(self, lease: Lease) -> EmailResult:
        if self.runtime_mode is not RuntimeMode.LIVE:
            raise OutreachActionError("email_send_requires_live_mode")
        outreach_id = lease.payload.get("outreach_id")
        approval_id = lease.payload.get("approval_id")
        attachments = lease.payload.get("attachment_ids", [])
        if (
            not isinstance(outreach_id, str)
            or not isinstance(approval_id, str)
            or not isinstance(attachments, list)
            or not all(isinstance(item, str) for item in attachments)
        ):
            raise OutreachActionError("email_send_payload_invalid")
        attachment_ids = tuple(attachments)
        expired = changed = False
        with self.queue.fence(lease) as session:
            task = self.queue._get(session, lease.task_id)
            draft = session.get(OutreachDraft, outreach_id)
            approval = session.get(Approval, approval_id)
            if (
                task.task_type != "SEND_OUTREACH_EMAIL"
                or not draft
                or draft.application_id != task.application_id
                or draft.approval_id != approval_id
                or not approval
                or approval.approval_type != "SEND_EMAIL"
                or approval.task_id != lease.task_id
                or approval.application_id != draft.application_id
            ):
                raise OutreachActionError("email_send_correlation_invalid")
            if approval.approval_status == "CONSUMED":
                digest = approval.payload_sha256
                key = f"send-email:{outreach_id}:{digest}"
                existing = session.scalar(
                    select(ExternalAction).where(ExternalAction.idempotency_key == key)
                )
                if (
                    not existing
                    or existing.action_type != "SEND_EMAIL"
                    or existing.approval_id != approval_id
                    or existing.request_sha256 != digest
                ):
                    raise OutreachActionError("consumed_email_action_missing")
                if existing.action_status == "SUCCEEDED":
                    return EmailResult(EmailOutcome.CONFIRMED, existing.external_reference)
                raise OutreachActionError("email_send_result_requires_reconciliation")
            if approval.approval_status != "APPROVED":
                raise OutreachActionError(f"email_approval_{approval.approval_status.lower()}")
            if (
                not approval.decided_by_slack_user_id
                or approval.decided_by_slack_user_id not in self.authorized_user_ids
            ):
                raise OutreachActionError("email_approver_not_authorized")
            if not approval.valid_until or self.queue.clock.now() >= datetime.fromisoformat(
                approval.valid_until.replace("Z", "+00:00")
            ):
                approval.approval_status = "EXPIRED"
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "email_approval_expired",
                        task_id=lease.task_id,
                        application_id=draft.application_id,
                        old_state="APPROVED",
                        new_state="EXPIRED",
                        metadata={"approval_id": approval_id},
                    )
                )
                expired = True
            try:
                payload = email_payload(session, outreach_id, attachment_ids)
            except ValueError:
                changed = True
                digest = approval.payload_sha256
                key = ""
            else:
                digest = email_sha256(payload)
                key = f"send-email:{outreach_id}:{digest}"
                try:
                    stored = read_review(approval.payload_path, approval.payload_sha256)
                except Exception:
                    changed = True
                else:
                    changed = approval.payload_sha256 != digest or canonical_email(
                        stored
                    ) != canonical_email(approval_value(payload))
            if changed:
                self._revoke(session, lease, approval, draft)
            if expired or changed:
                provider_draft_id = action_id = key = ""
            else:
                existing = session.scalar(
                    select(ExternalAction).where(ExternalAction.idempotency_key == key)
                )
                draft_action = session.scalar(
                    select(ExternalAction).where(
                        ExternalAction.application_id == draft.application_id,
                        ExternalAction.action_type == "GMAIL_CREATE_DRAFT",
                        ExternalAction.request_sha256 == digest,
                        ExternalAction.action_status == "SUCCEEDED",
                    )
                )
                if not draft_action or not draft_action.external_reference:
                    raise OutreachActionError("confirmed_gmail_draft_missing")
                provider_draft_id = draft_action.external_reference
                if existing:
                    if existing.action_status == "SUCCEEDED":
                        return EmailResult(EmailOutcome.CONFIRMED, existing.external_reference)
                    raise OutreachActionError("email_send_result_requires_reconciliation")
                now = format_utc(self.queue.clock.now())
                action_id = self.queue.ids.generate_ulid()
                session.add(
                    ExternalAction(
                        external_action_id=action_id,
                        application_id=draft.application_id,
                        task_id=lease.task_id,
                        approval_id=approval_id,
                        action_type="SEND_EMAIL",
                        idempotency_key=key,
                        request_sha256=digest,
                        action_status="EXECUTING",
                        result_json=json.dumps({"outreach_id": outreach_id}),
                        created_at=now,
                        updated_at=now,
                    )
                )
                approval.approval_status = "CONSUMED"
                approval.consumed_at = now
                draft.draft_status = "APPROVED"
                draft.updated_at = now
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "email_send_started",
                        task_id=lease.task_id,
                        application_id=draft.application_id,
                        old_state="READY_FOR_REVIEW",
                        new_state="APPROVED",
                        metadata={
                            "external_action_id": action_id,
                            "approval_id": approval_id,
                        },
                    )
                )
        if expired:
            raise OutreachActionError("email_approval_expired")
        if changed:
            raise OutreachActionError("email_payload_changed")
        try:
            result = await self.adapter.send_draft(provider_draft_id, key)
        except Exception:
            result = EmailResult(EmailOutcome.UNKNOWN)
        with self.queue.fence(lease) as session:
            action = session.get(ExternalAction, action_id)
            draft = session.get(OutreachDraft, outreach_id)
            assert action and draft
            action.action_status = (
                "SUCCEEDED" if result.outcome is EmailOutcome.CONFIRMED else "UNKNOWN_RESULT"
            )
            action.external_reference = result.provider_id
            action.updated_at = draft.updated_at = format_utc(self.queue.clock.now())
            if result.outcome is EmailOutcome.CONFIRMED:
                draft.draft_status = "SENT"
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "email_send_finished",
                    task_id=lease.task_id,
                    application_id=draft.application_id,
                    new_state=action.action_status,
                    metadata={"external_action_id": action_id, "outreach_id": outreach_id},
                )
            )
        return result

    def recover(
        self, *, action_types: tuple[str, ...] = ("GMAIL_CREATE_DRAFT", "SEND_EMAIL")
    ) -> int:
        """Never repeat an abandoned Gmail mutation without explicit reconciliation."""
        with self.queue.database.transaction(immediate=True) as session:
            rows = list(
                session.scalars(
                    select(ExternalAction).where(
                        ExternalAction.action_type.in_(action_types),
                        ExternalAction.action_status == "EXECUTING",
                    )
                )
            )
            now = format_utc(self.queue.clock.now())
            for row in rows:
                row.action_status = "UNKNOWN_RESULT"
                row.updated_at = now
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "gmail_action_recovered_unknown",
                        task_id=row.task_id,
                        application_id=row.application_id,
                        old_state="EXECUTING",
                        new_state="UNKNOWN_RESULT",
                        metadata={"external_action_id": row.external_action_id},
                    )
                )
            return len(rows)
