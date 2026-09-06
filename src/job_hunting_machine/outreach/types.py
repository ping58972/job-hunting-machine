"""Public-contact and email transport contracts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PublicPage:
    url: str
    status: int
    html: str


class ContactReader(Protocol):
    async def read(self, url: str) -> PublicPage: ...


@dataclass(frozen=True, slots=True)
class DiscoveredContact:
    source_url: str
    full_name: str | None
    title: str | None
    contact_type: str
    email: str | None
    linkedin_url: str | None
    confidence: float


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    artifact_id: str
    filename: str
    mime_type: str
    sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class EmailPayload:
    recipient: str
    subject: str
    body: str
    attachments: tuple[EmailAttachment, ...] = ()


class EmailOutcome(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EmailResult:
    outcome: EmailOutcome
    provider_id: str | None = None


class GmailAdapter(Protocol):
    async def create_draft(self, payload: EmailPayload, idempotency_key: str) -> EmailResult: ...

    async def send_draft(self, provider_draft_id: str, idempotency_key: str) -> EmailResult: ...
