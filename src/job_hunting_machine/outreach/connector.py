"""Connector Agent: public evidence, ranked contacts, drafts, and email approvals."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import (
    ApplicationDetails,
    ApplicationPipeline,
    Contact,
    Job,
)
from job_hunting_machine.database.repositories import (
    ApprovalCreate,
    ApprovalRepository,
    ContactRepository,
    ContactUpsert,
    OutreachCreate,
    OutreachDraftRepository,
    TaskCreate,
)
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.ids import IdKind
from job_hunting_machine.orchestration.checkpoints import run_blocking
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.orchestration.worker import Worker
from job_hunting_machine.orchestration.workflows import fake_workflow
from job_hunting_machine.outreach.actions import ConnectorExternalActionService
from job_hunting_machine.outreach.discovery import ContactDiscovery
from job_hunting_machine.outreach.drafts import email_draft, linkedin_manual_draft
from job_hunting_machine.outreach.email import approval_value, canonical_email, email_payload
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard
from job_hunting_machine.slack.control import SlackControlPlane


class ConnectorError(RuntimeError):
    """The connector cannot safely create contact or outreach records."""


_RANK = {"RECRUITER": 0, "HIRING_MANAGER": 1, "HR": 2, "EMPLOYEE": 3, "OTHER": 4}


class ConnectorWorker(Worker):
    def __init__(
        self,
        queue: QueueService,
        discovery: ContactDiscovery,
        actions: ConnectorExternalActionService,
        *,
        slack: SlackControlPlane | None = None,
        checkpoint_path: Path | None = None,
    ) -> None:
        super().__init__(
            queue,
            {"CONNECT_CONTACTS": fake_workflow()},
            checkpoint_path=checkpoint_path
            or queue.database.path.parent / "langgraph-checkpoints.db",
        )
        self.discovery = discovery
        self.actions = actions
        self.slack = slack

    async def startup(self) -> int:
        return await super().startup() + await run_blocking(self.actions.recover)

    async def _execute(self, lease: Lease) -> None:
        application_id = lease.payload.get("application_id")
        source_urls = lease.payload.get("source_urls", [])
        attachment_values = lease.payload.get("email_attachment_ids", [])
        if (
            not isinstance(application_id, str)
            or not isinstance(source_urls, list)
            or not all(isinstance(item, str) for item in source_urls)
            or not isinstance(attachment_values, list)
            or not all(isinstance(item, str) for item in attachment_values)
        ):
            raise ConnectorError("connector_payload_invalid")
        captures = [await self.discovery.capture(application_id, url) for url in source_urls]
        with self.queue.fence(lease) as session:
            task = self.queue._get(session, lease.task_id)
            app = session.get(ApplicationPipeline, application_id)
            details = session.get(ApplicationDetails, application_id)
            job = session.get(Job, app.job_id) if app else None
            if (
                task.application_id != application_id
                or not app
                or not details
                or not job
                or app.application_status != "SUBMITTED"
            ):
                raise ConnectorError("connector_application_state_invalid")
            repository = ContactRepository(session, self.queue.clock, self.queue.ids)
            for capture in captures:
                for item in capture.contacts:
                    row = repository.upsert(
                        ContactUpsert(
                            application_id,
                            details.company_name,
                            item.source_url,
                            item.full_name,
                            item.title,
                            item.contact_type,
                            item.email,
                            item.linkedin_url,
                            item.confidence,
                            True,
                        )
                    )
                    ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                        ActivityEvent(
                            "contact_evidence_captured",
                            task_id=lease.task_id,
                            application_id=application_id,
                            metadata={
                                "contact_id": row.contact_id,
                                "evidence_path": capture.path,
                                "evidence_sha256": capture.sha256,
                            },
                        )
                    )
            contacts = sorted(
                repository.for_application(application_id),
                key=lambda row: (
                    _RANK[row.contact_type or "OTHER"],
                    -(row.confidence or 0),
                    row.contact_id,
                ),
            )
            draft_repository = OutreachDraftRepository(session, self.queue.clock, self.queue.ids)
            outreach: list[tuple[str, str]] = []
            for contact in contacts:
                if not contact.verified:
                    continue
                if contact.email:
                    value = email_draft(contact, app, details, job)
                    draft_row = draft_repository.create(
                        OutreachCreate(
                            application_id,
                            contact.contact_id,
                            "EMAIL",
                            value.body,
                            value.subject,
                        )
                    )
                    outreach.append((draft_row.outreach_id, "EMAIL"))
                if contact.linkedin_url:
                    value = linkedin_manual_draft(contact, app, details, job)
                    draft_row = draft_repository.create(
                        OutreachCreate(
                            application_id,
                            contact.contact_id,
                            "LINKEDIN_MANUAL",
                            value.body,
                        )
                    )
                    outreach.append((draft_row.outreach_id, "LINKEDIN_MANUAL"))
        approvals: list[str] = []
        attachment_ids = tuple(attachment_values)
        for outreach_id, channel in outreach:
            if channel == "EMAIL":
                await self.actions.create_draft(lease, outreach_id, attachment_ids)
                approval_id = self._approval(lease, outreach_id, attachment_ids)
            else:
                approval_id = self._manual_approval(lease, outreach_id)
            approvals.append(approval_id)
            if self.slack:
                self.slack.request_approval(approval_id)
        self.queue.complete(
            lease,
            {
                "contacts": sum(len(item.contacts) for item in captures),
                "outreach_drafts": len(outreach),
                "email_approvals": approvals,
            },
        )

    def _approval(self, lease: Lease, outreach_id: str, attachment_ids: tuple[str, ...]) -> str:
        approval_id = self.queue.ids.new(IdKind.APPROVAL)
        task_id = self.queue.ids.new(IdKind.TASK)
        with self.queue.fence(lease) as session:
            draft = OutreachDraftRepository(session, self.queue.clock, self.queue.ids).get(
                outreach_id
            )
            if not draft or draft.application_id != lease.payload.get("application_id"):
                raise ConnectorError("outreach_approval_draft_mismatch")
            payload = email_payload(session, outreach_id, attachment_ids)
            content = canonical_email(approval_value(payload))
            digest = hashlib.sha256(content).hexdigest()
            directory = PathGuard().mkdir(
                PROJECT_ROOT / "applications" / draft.application_id / "outreach" / outreach_id,
                parents=True,
                exist_ok=True,
            )
            path = PathGuard().write_bytes(directory / f"{approval_id}.json", content)
            interrupt_id = self.queue.ids.generate_ulid()
            task = self.queue.enqueue_waiting_in_transaction(
                session,
                TaskCreate(
                    "SEND_OUTREACH_EMAIL",
                    application_id=draft.application_id,
                    parent_task_id=lease.task_id,
                    dedupe_key=f"send_email:{outreach_id}:{digest}",
                    payload={
                        "application_id": draft.application_id,
                        "outreach_id": outreach_id,
                        "approval_id": approval_id,
                        "attachment_ids": list(attachment_ids),
                    },
                    task_id=task_id,
                ),
                {
                    "interrupts": {
                        interrupt_id: {
                            "kind": "SEND_EMAIL_APPROVAL",
                            "approval_id": approval_id,
                            "outreach_id": outreach_id,
                            "payload_sha256": digest,
                        }
                    }
                },
            )
            approval = ApprovalRepository(session, self.queue.clock, self.queue.ids).create(
                ApprovalCreate(
                    "SEND_EMAIL",
                    str(path.relative_to(PROJECT_ROOT)),
                    digest,
                    draft.application_id,
                    task.task_id,
                    format_utc(self.queue.clock.now() + timedelta(hours=24)),
                    approval_id,
                )
            )
            OutreachDraftRepository(session, self.queue.clock, self.queue.ids).bind_approval(
                outreach_id, approval
            )
            return approval.approval_id

    def _manual_approval(self, lease: Lease, outreach_id: str) -> str:
        approval_id = self.queue.ids.new(IdKind.APPROVAL)
        task_id = self.queue.ids.new(IdKind.TASK)
        with self.queue.fence(lease) as session:
            repository = OutreachDraftRepository(session, self.queue.clock, self.queue.ids)
            draft = repository.get(outreach_id)
            contact = session.get(Contact, draft.contact_id) if draft else None
            if (
                not draft
                or draft.channel != "LINKEDIN_MANUAL"
                or not contact
                or not contact.linkedin_url
            ):
                raise ConnectorError("manual_outreach_draft_invalid")
            value = {
                "application_id": draft.application_id,
                "outreach_id": outreach_id,
                "recipient_linkedin_url": contact.linkedin_url,
                "body": draft.body,
            }
            content = canonical_email(value)
            digest = hashlib.sha256(content).hexdigest()
            directory = PathGuard().mkdir(
                PROJECT_ROOT / "applications" / draft.application_id / "outreach" / outreach_id,
                parents=True,
                exist_ok=True,
            )
            path = PathGuard().write_bytes(directory / f"{approval_id}.json", content)
            interrupt_id = self.queue.ids.generate_ulid()
            task = self.queue.enqueue_waiting_in_transaction(
                session,
                TaskCreate(
                    "REVIEW_LINKEDIN_OUTREACH",
                    application_id=draft.application_id,
                    parent_task_id=lease.task_id,
                    dedupe_key=f"manual_outreach:{outreach_id}:{digest}",
                    payload={
                        "application_id": draft.application_id,
                        "outreach_id": outreach_id,
                        "approval_id": approval_id,
                        "payload_sha256": digest,
                    },
                    task_id=task_id,
                ),
                {
                    "interrupts": {
                        interrupt_id: {
                            "kind": "LINKEDIN_MANUAL_APPROVAL",
                            "approval_id": approval_id,
                            "outreach_id": outreach_id,
                            "payload_sha256": digest,
                        }
                    }
                },
            )
            approval = ApprovalRepository(session, self.queue.clock, self.queue.ids).create(
                ApprovalCreate(
                    "SEND_EXTERNAL_MESSAGE",
                    str(path.relative_to(PROJECT_ROOT)),
                    digest,
                    draft.application_id,
                    task.task_id,
                    format_utc(self.queue.clock.now() + timedelta(hours=24)),
                    approval_id,
                )
            )
            repository.bind_approval(outreach_id, approval)
            return approval.approval_id


def contact_json(contact: Contact) -> str:
    """Local inspection helper with explicit public-contact fields."""
    return json.dumps(
        {
            "contact_id": contact.contact_id,
            "application_id": contact.application_id,
            "full_name": contact.full_name,
            "title": contact.title,
            "contact_type": contact.contact_type,
            "email": contact.email,
            "linkedin_url": contact.linkedin_url,
            "source_url": contact.source_url,
        },
        sort_keys=True,
    )
