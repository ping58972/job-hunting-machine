"""Audited contact and outreach-draft persistence over Architecture v2 tables."""

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import or_, select

from job_hunting_machine.database.models import (
    ApplicationDetails,
    ApplicationPipeline,
    Approval,
    Contact,
    OutreachDraft,
)
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import ReplayConflictError, Repository
from job_hunting_machine.ids import IdKind, validate_id

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CONTACT_TYPES = frozenset({"RECRUITER", "HR", "HIRING_MANAGER", "EMPLOYEE", "OTHER"})
_CHANNELS = frozenset({"EMAIL", "LINKEDIN_MANUAL"})


def normalized_email(value: str | None) -> str | None:
    if value is None:
        return None
    result = value.strip().lower()
    if _EMAIL.fullmatch(result) is None:
        raise ValueError("invalid_contact_email")
    return result


def normalized_public_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid_public_contact_url")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", ""))


def outreach_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ContactUpsert:
    application_id: str
    company_name: str
    source_url: str
    full_name: str | None = None
    title: str | None = None
    contact_type: str = "OTHER"
    email: str | None = None
    linkedin_url: str | None = None
    confidence: float = 0.0
    verified: bool = False


class ContactRepository(Repository):
    def for_application(self, application_id: str) -> list[Contact]:
        validate_id(application_id, IdKind.APPLICATION)
        return list(
            self.session.scalars(
                select(Contact)
                .where(Contact.application_id == application_id)
                .order_by(Contact.contact_id)
            )
        )

    def upsert(self, data: ContactUpsert) -> Contact:
        validate_id(data.application_id, IdKind.APPLICATION)
        if data.contact_type not in _CONTACT_TYPES or not 0 <= data.confidence <= 1:
            raise ValueError("invalid_contact_classification")
        app = self.session.get(ApplicationPipeline, data.application_id)
        details = self.session.get(ApplicationDetails, data.application_id)
        if not app or not details or data.company_name.strip() != details.company_name:
            raise ValueError("contact_application_company_mismatch")
        email = normalized_email(data.email)
        linkedin = normalized_public_url(data.linkedin_url)
        if linkedin:
            host = urlsplit(linkedin).hostname or ""
            if host != "linkedin.com" and not host.endswith(".linkedin.com"):
                raise ValueError("linkedin_contact_url_invalid")
        source = normalized_public_url(data.source_url)
        if not email and not linkedin:
            raise ValueError("contact_requires_public_identifier")
        conditions = []
        if email:
            conditions.append(Contact.email == email)
        if linkedin:
            conditions.append(Contact.linkedin_url == linkedin)
        existing = self.session.scalar(
            select(Contact).where(Contact.application_id == data.application_id, or_(*conditions))
        )
        now = self.timestamp()
        if existing:
            old_state = "VERIFIED" if existing.verified else "UNVERIFIED"
            for current, incoming in (
                (existing.company_name, data.company_name.strip()),
                (existing.full_name, data.full_name),
                (existing.title, data.title),
                (existing.contact_type, data.contact_type),
                (existing.email, email),
                (existing.linkedin_url, linkedin),
            ):
                if current is not None and incoming is not None and current != incoming:
                    raise ReplayConflictError("public_contact_identity_conflict")
            existing.full_name = existing.full_name or data.full_name
            existing.title = existing.title or data.title
            existing.email = existing.email or email
            existing.linkedin_url = existing.linkedin_url or linkedin
            existing.confidence = max(existing.confidence or 0, data.confidence)
            existing.verified = bool(existing.verified or data.verified)
            existing.updated_at = now
            self.session.flush()
            ActivityLogRepository(self.session, self.clock, self.ids).append(
                ActivityEvent(
                    "contact_evidence_updated",
                    application_id=data.application_id,
                    old_state=old_state,
                    new_state="VERIFIED" if existing.verified else "UNVERIFIED",
                    metadata={"contact_id": existing.contact_id},
                )
            )
            return existing
        row = Contact(
            contact_id=self.ids.new(IdKind.CONTACT),
            application_id=data.application_id,
            company_name=data.company_name.strip(),
            full_name=data.full_name.strip() if data.full_name else None,
            title=data.title.strip() if data.title else None,
            contact_type=data.contact_type,
            email=email,
            linkedin_url=linkedin,
            source_url=source,
            confidence=data.confidence,
            verified=data.verified,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self.session.flush()
        ActivityLogRepository(self.session, self.clock, self.ids).append(
            ActivityEvent(
                "contact_discovered",
                application_id=data.application_id,
                new_state="VERIFIED" if data.verified else "UNVERIFIED",
                metadata={"contact_id": row.contact_id, "contact_type": row.contact_type},
            )
        )
        return row


@dataclass(frozen=True, slots=True)
class OutreachCreate:
    application_id: str
    contact_id: str
    channel: str
    body: str
    subject: str | None = None


class OutreachDraftRepository(Repository):
    def get(self, outreach_id: str) -> OutreachDraft | None:
        return self.session.get(OutreachDraft, outreach_id)

    def for_application(self, application_id: str) -> list[OutreachDraft]:
        validate_id(application_id, IdKind.APPLICATION)
        return list(
            self.session.scalars(
                select(OutreachDraft)
                .where(OutreachDraft.application_id == application_id)
                .order_by(OutreachDraft.outreach_id)
            )
        )

    def create(self, data: OutreachCreate) -> OutreachDraft:
        validate_id(data.application_id, IdKind.APPLICATION)
        validate_id(data.contact_id, IdKind.CONTACT)
        if data.channel not in _CHANNELS or not data.body.strip():
            raise ValueError("invalid_outreach_draft")
        if data.channel == "EMAIL" and not data.subject:
            raise ValueError("email_subject_required")
        contact = self.session.get(Contact, data.contact_id)
        if not contact or contact.application_id != data.application_id:
            raise ValueError("outreach_contact_application_mismatch")
        payload = {
            "application_id": data.application_id,
            "contact_id": data.contact_id,
            "channel": data.channel,
            "subject": data.subject,
            "body": data.body,
        }
        digest = outreach_hash(payload)
        existing = self.session.scalar(
            select(OutreachDraft).where(
                OutreachDraft.application_id == data.application_id,
                OutreachDraft.contact_id == data.contact_id,
                OutreachDraft.channel == data.channel,
            )
        )
        if existing:
            if existing.payload_sha256 != digest:
                raise ReplayConflictError("outreach_draft_already_exists_with_different_content")
            return existing
        now = self.timestamp()
        row = OutreachDraft(
            outreach_id=self.ids.generate_ulid(),
            application_id=data.application_id,
            contact_id=data.contact_id,
            channel=data.channel,
            subject=data.subject,
            body=data.body,
            payload_sha256=digest,
            draft_status="READY_FOR_REVIEW",
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self.session.flush()
        ActivityLogRepository(self.session, self.clock, self.ids).append(
            ActivityEvent(
                "outreach_draft_created",
                application_id=data.application_id,
                new_state="READY_FOR_REVIEW",
                metadata={"outreach_id": row.outreach_id, "channel": row.channel},
            )
        )
        return row

    def bind_approval(self, outreach_id: str, approval: Approval) -> OutreachDraft:
        row = self.get(outreach_id)
        if (
            not row
            or approval.application_id != row.application_id
            or (row.channel == "EMAIL" and approval.approval_type != "SEND_EMAIL")
            or (
                row.channel == "LINKEDIN_MANUAL"
                and approval.approval_type != "SEND_EXTERNAL_MESSAGE"
            )
        ):
            raise ValueError("outreach_approval_correlation_invalid")
        if row.approval_id and row.approval_id != approval.approval_id:
            raise ReplayConflictError("outreach_already_bound_to_approval")
        row.approval_id = approval.approval_id
        row.updated_at = self.timestamp()
        return row
