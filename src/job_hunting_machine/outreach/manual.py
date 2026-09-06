"""Approve LinkedIn-manual drafts without opening or controlling LinkedIn."""

import hashlib
from pathlib import Path

from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import Approval, Contact, OutreachDraft
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.orchestration.checkpoints import run_blocking
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.orchestration.worker import Worker
from job_hunting_machine.orchestration.workflows import fake_workflow
from job_hunting_machine.outreach.email import canonical_email
from job_hunting_machine.submission.review import read_review


class ManualOutreachWorker(Worker):
    """Validate a manual draft decision and perform no external action."""

    def __init__(self, queue: QueueService, *, checkpoint_path: Path | None = None) -> None:
        super().__init__(
            queue,
            {"REVIEW_LINKEDIN_OUTREACH": fake_workflow()},
            checkpoint_path=checkpoint_path
            or queue.database.path.parent / "langgraph-checkpoints.db",
        )

    async def _execute(self, lease: Lease) -> None:
        outreach_id = lease.payload.get("outreach_id")
        approval_id = lease.payload.get("approval_id")
        if not isinstance(outreach_id, str) or not isinstance(approval_id, str):
            raise ValueError("manual_outreach_payload_invalid")
        waiting = changed = False
        status = "READY_FOR_REVIEW"
        with self.queue.fence(lease) as session:
            draft = session.get(OutreachDraft, outreach_id)
            contact = session.get(Contact, draft.contact_id) if draft else None
            approval = session.get(Approval, approval_id)
            if (
                not draft
                or not contact
                or not contact.linkedin_url
                or not approval
                or approval.approval_type != "SEND_EXTERNAL_MESSAGE"
                or approval.task_id != lease.task_id
                or approval.application_id != draft.application_id
            ):
                raise ValueError("manual_outreach_correlation_invalid")
            if approval.approval_status == "REJECTED":
                draft.draft_status = status = "REJECTED"
            elif approval.approval_status == "APPROVED":
                value = {
                    "application_id": draft.application_id,
                    "outreach_id": outreach_id,
                    "recipient_linkedin_url": contact.linkedin_url,
                    "body": draft.body,
                }
                content = canonical_email(value)
                try:
                    stored = canonical_email(
                        read_review(approval.payload_path, approval.payload_sha256)
                    )
                except Exception:
                    changed = True
                else:
                    changed = (
                        hashlib.sha256(content).hexdigest() != approval.payload_sha256
                        or stored != content
                    )
                if changed:
                    approval.approval_status = "REVOKED"
                    approval.rejection_reason = "manual_outreach_payload_changed"
                else:
                    draft.draft_status = status = "APPROVED"
            else:
                waiting = True
            draft.updated_at = format_utc(self.queue.clock.now())
            if not waiting:
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "manual_outreach_reviewed",
                        task_id=lease.task_id,
                        application_id=draft.application_id,
                        new_state="REVOKED" if changed else status,
                        metadata={"outreach_id": outreach_id},
                    )
                )
        if waiting or changed:
            await run_blocking(
                self.queue.complete,
                lease,
                {
                    "interrupts": {
                        "manual": {
                            "kind": (
                                "MANUAL_OUTREACH_CHANGED" if changed else "MANUAL_APPROVAL_REQUIRED"
                            )
                        }
                    }
                },
                waiting=True,
            )
            return
        await run_blocking(
            self.queue.complete,
            lease,
            {"outreach_id": outreach_id, "external_send": False, "status": status},
        )
