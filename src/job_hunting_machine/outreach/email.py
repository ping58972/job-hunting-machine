"""Canonical SEND_EMAIL payloads bound to exact draft and attachment bytes."""

import hashlib
import json
from collections.abc import Mapping

from sqlalchemy.orm import Session

from job_hunting_machine.browser.artifacts import select_artifact
from job_hunting_machine.database.models import Contact, OutreachDraft
from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.outreach.types import EmailAttachment, EmailPayload

_ATTACHMENT_TYPES = frozenset({"RESUME_PDF", "COVER_LETTER_PDF", "TRANSCRIPT"})


def email_payload(
    session: Session, outreach_id: str, attachment_ids: tuple[str, ...] = ()
) -> EmailPayload:
    draft = session.get(OutreachDraft, outreach_id)
    if not draft or draft.channel != "EMAIL" or not draft.subject:
        raise ValueError("email_outreach_missing")
    contact = session.get(Contact, draft.contact_id)
    if not contact or contact.application_id != draft.application_id or not contact.email:
        raise ValueError("email_contact_invalid")
    attachments: list[EmailAttachment] = []
    for artifact_id in attachment_ids:
        validate_id(artifact_id, IdKind.ARTIFACT)
        from job_hunting_machine.database.models import Artifact

        row = session.get(Artifact, artifact_id)
        if not row or row.artifact_type not in _ATTACHMENT_TYPES:
            raise ValueError("email_attachment_type_invalid")
        selected = select_artifact(session, draft.application_id, artifact_id, row.artifact_type)
        attachments.append(
            EmailAttachment(
                selected.artifact_id,
                selected.path.name,
                selected.mime_type,
                selected.sha256,
                selected.content,
            )
        )
    return EmailPayload(contact.email, draft.subject, draft.body, tuple(attachments))


def approval_value(payload: EmailPayload) -> dict[str, object]:
    return {
        "recipient": payload.recipient,
        "subject": payload.subject,
        "body": payload.body,
        "attachments": [
            {
                "artifact_id": item.artifact_id,
                "filename": item.filename,
                "mime_type": item.mime_type,
                "sha256": item.sha256,
            }
            for item in payload.attachments
        ],
    }


def canonical_email(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def email_sha256(payload: EmailPayload) -> str:
    return hashlib.sha256(canonical_email(approval_value(payload))).hexdigest()
