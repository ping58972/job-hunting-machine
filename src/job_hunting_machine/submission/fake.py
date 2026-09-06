"""Network-free ATS submission double with deterministic lost-response behavior."""

from dataclasses import dataclass, field
from typing import Literal

from job_hunting_machine.submission.types import (
    SubmissionAdapter,
    SubmissionOutcome,
    SubmissionResult,
)


@dataclass
class FakeSubmissionAdapter(SubmissionAdapter):
    mode: Literal["confirmed", "lost_after_click", "unknown_without_submit"] = "confirmed"
    submit_calls: int = 0
    reconcile_calls: int = 0
    submitted: set[str] = field(default_factory=set)

    async def submit(self, application_id: str, review: dict[str, object]) -> SubmissionResult:
        self.submit_calls += 1
        if self.mode == "unknown_without_submit":
            return SubmissionResult(SubmissionOutcome.UNKNOWN)
        self.submitted.add(application_id)
        if self.mode == "lost_after_click":
            return SubmissionResult(SubmissionOutcome.UNKNOWN)
        return SubmissionResult(SubmissionOutcome.CONFIRMED, f"fake-confirmation:{application_id}")

    async def reconcile(self, application_id: str, review: dict[str, object]) -> SubmissionResult:
        self.reconcile_calls += 1
        if application_id in self.submitted:
            return SubmissionResult(
                SubmissionOutcome.CONFIRMED, f"fake-confirmation:{application_id}"
            )
        return SubmissionResult(SubmissionOutcome.NOT_SUBMITTED)
