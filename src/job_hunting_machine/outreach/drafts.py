"""Evidence-honest email and LinkedIn-manual draft generation."""

from dataclasses import dataclass

from job_hunting_machine.database.models import (
    ApplicationDetails,
    ApplicationPipeline,
    Contact,
    Job,
)


@dataclass(frozen=True, slots=True)
class DraftText:
    subject: str | None
    body: str


def email_draft(
    contact: Contact, app: ApplicationPipeline, details: ApplicationDetails, job: Job
) -> DraftText:
    if (
        contact.application_id != app.application_id
        or details.application_id != app.application_id
        or job.job_id != app.job_id
    ):
        raise ValueError("draft_application_mismatch")
    greeting = f"Hello {contact.full_name.split()[0]}," if contact.full_name else "Hello,"
    subject = f"Interest in {details.job_title} at {details.company_name}"
    body = (
        f"{greeting}\n\n"
        f"I recently applied for the {details.job_title} position at {details.company_name}. "
        "I am writing to express my interest and ask whether you can share any guidance about "
        "the role or hiring process.\n\nThank you for your time."
    )
    return DraftText(subject, body)


def linkedin_manual_draft(
    contact: Contact, app: ApplicationPipeline, details: ApplicationDetails, job: Job
) -> DraftText:
    if (
        contact.application_id != app.application_id
        or details.application_id != app.application_id
        or job.job_id != app.job_id
    ):
        raise ValueError("draft_application_mismatch")
    greeting = f"Hi {contact.full_name.split()[0]}," if contact.full_name else "Hello,"
    body = (
        f"{greeting} I recently applied for the {details.job_title} role at "
        f"{details.company_name}. I would appreciate any guidance you can share about the role."
    )
    return DraftText(None, body)
