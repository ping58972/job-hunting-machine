"""The narrow final-submission port, owned only by the Submission Agent."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class SubmissionOutcome(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    NOT_SUBMITTED = "NOT_SUBMITTED"


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    outcome: SubmissionOutcome
    external_reference: str | None = None


class SubmissionAdapter(Protocol):
    """A final-click capability that is never exposed to the Form Agent."""

    async def submit(self, application_id: str, review: dict[str, object]) -> SubmissionResult: ...

    async def reconcile(
        self, application_id: str, review: dict[str, object]
    ) -> SubmissionResult: ...
