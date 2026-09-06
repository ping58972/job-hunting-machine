"""Application matching and Luna-backed status classification."""

import json
import re

from job_hunting_machine.models.gateway import ModelGateway
from job_hunting_machine.models.router import Tier
from job_hunting_machine.monitor.types import (
    ApplicationStatus,
    GmailMessage,
    PortalPage,
    StatusClassification,
    StatusClassifier,
)

_IGNORED_COMPANY_WORDS = frozenset(
    {"and", "company", "corp", "corporation", "group", "inc", "llc", "ltd", "technologies"}
)


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def matches_application(
    message: GmailMessage, application_id: str, company_name: str, job_title: str
) -> bool:
    """Require explicit application ID or a company token; title alone is insufficient."""
    combined = f"{message.sender} {message.subject} {message.body}"
    if application_id.casefold() in combined.casefold():
        return True
    company = {
        word
        for word in _words(company_name)
        if len(word) >= 3 and word not in _IGNORED_COMPANY_WORDS
    }
    if not company or not company.intersection(_words(combined)):
        return False
    title = {word for word in _words(job_title) if len(word) >= 4}
    # A sender-domain company match is sufficient; otherwise require a job-title clue.
    sender_words = _words(message.sender)
    return bool(company.intersection(sender_words) or title.intersection(_words(combined)))


def _keyword_classification(text: str) -> StatusClassification:
    value = " ".join(text.casefold().split())
    rules: tuple[tuple[ApplicationStatus, tuple[str, ...], str], ...] = (
        (
            ApplicationStatus.REJECTED,
            (
                "not moving forward",
                "will not be moving forward",
                "other candidates",
                "position has been filled",
                "regret to inform",
                "application was unsuccessful",
            ),
            "explicit_rejection",
        ),
        (
            ApplicationStatus.CANCELED,
            ("position has been canceled", "position has been cancelled", "role was canceled"),
            "explicit_cancellation",
        ),
        (
            ApplicationStatus.WITHDRAWN,
            ("application withdrawn", "you withdrew"),
            "explicit_withdrawal",
        ),
        (
            ApplicationStatus.FINAL_INTERVIEW,
            ("final interview", "final-round interview", "final round interview"),
            "final_interview_request",
        ),
        (
            ApplicationStatus.OFFER,
            ("offer letter", "offer of employment", "pleased to offer"),
            "explicit_offer",
        ),
        (
            ApplicationStatus.ASSESSMENT,
            ("coding assessment", "technical assessment", "complete the assessment"),
            "assessment_request",
        ),
        (
            ApplicationStatus.RECRUITER_SCREEN,
            ("recruiter screen", "phone screen", "recruiting screen"),
            "screen_request",
        ),
        (
            ApplicationStatus.INTERVIEW,
            ("schedule an interview", "interview invitation", "invite you to interview"),
            "interview_request",
        ),
        (
            ApplicationStatus.UNDER_REVIEW,
            ("under review", "reviewing your application"),
            "review_status",
        ),
        (
            ApplicationStatus.SUBMITTED,
            ("application received", "application submitted"),
            "submission_confirmation",
        ),
    )
    for status, phrases, code in rules:
        if any(phrase in value for phrase in phrases):
            return StatusClassification(
                detected_status=status, confidence=0.99, evidence_codes=(code,)
            )
    return StatusClassification(detected_status=None, confidence=0.0, evidence_codes=("unknown",))


class KeywordStatusClassifier:
    """Deterministic classifier used by tests and as a fail-closed first pass."""

    async def classify_email(
        self, task_id: str, application_id: str, message: GmailMessage
    ) -> StatusClassification:
        return _keyword_classification(f"{message.subject}\n{message.body}")

    async def classify_portal(
        self, task_id: str, application_id: str, page: PortalPage
    ) -> StatusClassification:
        return _keyword_classification(page.text)


class LunaStatusClassifier:
    """Semantic ambiguity boundary; ModelGateway routes both operations to Luna."""

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def _classify(
        self,
        task_id: str,
        application_id: str,
        operation: str,
        source: dict[str, object],
    ) -> StatusClassification:
        context = json.dumps(
            {"application_id": application_id, "source": source},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return await self.gateway.structured(
            task_id=task_id,
            operation=operation,
            agent_name="MonitorAgent",
            context=context,
            output_type=StatusClassification,
            prompt_name="monitor_status_classification",
            prompt_version="1.0.0",
            ceiling=Tier.LUNA,
        )

    async def classify_email(
        self, task_id: str, application_id: str, message: GmailMessage
    ) -> StatusClassification:
        return await self._classify(
            task_id,
            application_id,
            "email_status_classification",
            {"sender": message.sender, "subject": message.subject, "body": message.body[:50_000]},
        )

    async def classify_portal(
        self, task_id: str, application_id: str, page: PortalPage
    ) -> StatusClassification:
        return await self._classify(
            task_id,
            application_id,
            "portal_status_classification",
            {"url": page.url, "status_code": page.status_code, "text": page.text[:50_000]},
        )


class HybridStatusClassifier:
    """Use exact phrases first and Luna only when deterministic evidence is ambiguous."""

    def __init__(self, semantic: StatusClassifier | None = None) -> None:
        self.deterministic = KeywordStatusClassifier()
        self.semantic = semantic

    async def classify_email(
        self, task_id: str, application_id: str, message: GmailMessage
    ) -> StatusClassification:
        result = await self.deterministic.classify_email(task_id, application_id, message)
        if result.detected_status is not None or self.semantic is None:
            return result
        return await self.semantic.classify_email(task_id, application_id, message)

    async def classify_portal(
        self, task_id: str, application_id: str, page: PortalPage
    ) -> StatusClassification:
        result = await self.deterministic.classify_portal(task_id, application_id, page)
        if result.detected_status is not None or self.semantic is None:
            return result
        return await self.semantic.classify_portal(task_id, application_id, page)
