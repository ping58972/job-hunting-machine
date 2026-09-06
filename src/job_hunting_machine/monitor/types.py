"""Typed, read-only observations and monitor classification results."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ApplicationStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    ASSESSMENT = "ASSESSMENT"
    RECRUITER_SCREEN = "RECRUITER_SCREEN"
    INTERVIEW = "INTERVIEW"
    FINAL_INTERVIEW = "FINAL_INTERVIEW"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CANCELED = "CANCELED"


TERMINAL_STATUSES = frozenset({"REJECTED", "WITHDRAWN", "CANCELED", "CLOSED"})
DESTRUCTIVE_STATUSES = frozenset({"REJECTED", "WITHDRAWN", "CANCELED"})


@dataclass(frozen=True, slots=True)
class GmailMessage:
    provider_id: str
    sender: str
    subject: str
    body: str
    received_at: str
    thread_id: str | None = None


class GmailReader(Protocol):
    async def search(self, query: str, *, limit: int) -> list[GmailMessage]: ...


@dataclass(frozen=True, slots=True)
class PortalPage:
    url: str
    status_code: int
    text: str
    vendor: str | None = None


class PortalReader(Protocol):
    async def read(self, url: str) -> PortalPage: ...


class StatusClassification(BaseModel):
    """Strict ModelGateway output; evidence codes remain concise and non-sensitive."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    detected_status: ApplicationStatus | None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_codes: tuple[str, ...] = Field(max_length=8)


class StatusClassifier(Protocol):
    async def classify_email(
        self, task_id: str, application_id: str, message: GmailMessage
    ) -> StatusClassification: ...

    async def classify_portal(
        self, task_id: str, application_id: str, page: PortalPage
    ) -> StatusClassification: ...
