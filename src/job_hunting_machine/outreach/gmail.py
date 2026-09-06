"""Fake-by-default Gmail draft and send adapters."""

import base64
import os
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx

from job_hunting_machine.outreach.types import (
    EmailOutcome,
    EmailPayload,
    EmailResult,
    GmailAdapter,
)


@dataclass
class FakeGmailAdapter(GmailAdapter):
    draft_calls: int = 0
    send_calls: int = 0
    drafts: dict[str, EmailPayload] = field(default_factory=dict)
    sent: set[str] = field(default_factory=set)

    async def create_draft(self, payload: EmailPayload, idempotency_key: str) -> EmailResult:
        self.draft_calls += 1
        identifier = f"fake-draft-{len(self.drafts) + 1}"
        self.drafts[identifier] = payload
        return EmailResult(EmailOutcome.CONFIRMED, identifier)

    async def send_draft(self, provider_draft_id: str, idempotency_key: str) -> EmailResult:
        self.send_calls += 1
        if provider_draft_id not in self.drafts:
            return EmailResult(EmailOutcome.UNKNOWN)
        self.sent.add(provider_draft_id)
        return EmailResult(EmailOutcome.CONFIRMED, f"fake-message-{provider_draft_id}")


def _raw_message(payload: EmailPayload) -> str:
    message = EmailMessage()
    message["To"] = payload.recipient
    message["Subject"] = payload.subject
    message.set_content(payload.body)
    for item in payload.attachments:
        main, _, subtype = item.mime_type.partition("/")
        message.add_attachment(
            item.content,
            maintype=main or "application",
            subtype=subtype or "octet-stream",
            filename=item.filename,
        )
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


class GmailRestAdapter(GmailAdapter):
    """Minimal Gmail API adapter; construction requires an explicit live opt-in."""

    def __init__(self, access_token: str | None = None) -> None:
        token = access_token or os.environ.get("GMAIL_ACCESS_TOKEN")
        if os.environ.get("GMAIL_ALLOW_LIVE") != "1" or not token:
            raise ValueError("live_gmail_requires_opt_in_and_token")
        self._token = token
        self._base = "https://gmail.googleapis.com/gmail/v1/users/me"

    async def create_draft(self, payload: EmailPayload, idempotency_key: str) -> EmailResult:
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.post(
                    f"{self._base}/drafts",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={"message": {"raw": _raw_message(payload)}},
                )
                response.raise_for_status()
                value = response.json()
                identifier = value.get("id")
                if not isinstance(identifier, str):
                    return EmailResult(EmailOutcome.UNKNOWN)
                return EmailResult(EmailOutcome.CONFIRMED, identifier)
        except httpx.HTTPError:
            return EmailResult(EmailOutcome.UNKNOWN)

    async def send_draft(self, provider_draft_id: str, idempotency_key: str) -> EmailResult:
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.post(
                    f"{self._base}/drafts/send",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={"id": provider_draft_id},
                )
                response.raise_for_status()
                value = response.json()
                identifier = value.get("id")
                if not isinstance(identifier, str):
                    return EmailResult(EmailOutcome.UNKNOWN)
                return EmailResult(EmailOutcome.CONFIRMED, identifier)
        except httpx.HTTPError:
            return EmailResult(EmailOutcome.UNKNOWN)
