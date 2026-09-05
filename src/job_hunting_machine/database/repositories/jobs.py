"""Persist supplied job data; URL discovery and qualification are later phases."""

import hashlib
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from job_hunting_machine.database.models import Job
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import (
    ConcurrentUpdateError,
    RecordNotFoundError,
    ReplayConflictError,
    Repository,
    required_text,
)
from job_hunting_machine.ids import IdKind, validate_id

QUALIFICATION_STATUSES = frozenset({"NEW", "ACTIVE", "PASSED", "ABORTED", "NEEDS_REVIEW", "ERROR"})


@dataclass(frozen=True, slots=True)
class JobCreate:
    original_url: str
    canonical_url: str
    source_type: str
    company_name: str | None = None
    job_title: str | None = None
    source_reference: str | None = None
    qualification_status: str = "NEW"
    job_id: str | None = None


class JobRepository(Repository):
    def get(self, job_id: str) -> Job | None:
        return self.session.get(Job, validate_id(job_id, IdKind.JOB))

    def get_by_url(self, canonical_url: str) -> Job | None:
        return self.session.scalar(select(Job).where(Job.canonical_url == canonical_url))

    @staticmethod
    def _check_replay(existing: Job, data: JobCreate) -> Job:
        if data.job_id is not None and existing.job_id != data.job_id:
            raise ReplayConflictError("The canonical URL already has a different Job ID")
        return existing

    def create(self, data: JobCreate) -> Job:
        """Reuse an existing exact canonical URL without generating another ID."""
        required_text(data.original_url)
        required_text(data.canonical_url)
        required_text(data.source_type)
        if data.qualification_status not in QUALIFICATION_STATUSES:
            raise ValueError("Invalid qualification status")
        if data.job_id is not None:
            validate_id(data.job_id, IdKind.JOB)
        existing = self.get_by_url(data.canonical_url)
        if existing is not None:
            return self._check_replay(existing, data)
        try:
            with self.session.begin_nested():
                now = self.timestamp()
                record = Job(
                    job_id=data.job_id or self.ids.new(IdKind.JOB),
                    original_url=data.original_url,
                    canonical_url=data.canonical_url,
                    url_sha256=hashlib.sha256(data.canonical_url.encode("utf-8")).hexdigest(),
                    source_type=data.source_type,
                    source_reference=data.source_reference,
                    company_name=data.company_name,
                    job_title=data.job_title,
                    qualification_status=data.qualification_status,
                    first_seen_at=now,
                    updated_at=now,
                )
                self.session.add(record)
                self.session.flush()
                ActivityLogRepository(self.session, self.clock, self.ids).append(
                    ActivityEvent(
                        "job_created", job_id=record.job_id, new_state=record.qualification_status
                    )
                )
            return record
        except IntegrityError:
            existing = self.get_by_url(data.canonical_url)
            if existing is None:
                raise
            return self._check_replay(existing, data)

    def set_qualification_status(self, job_id: str, status: str, *, expected_status: str) -> Job:
        """Store a caller-supplied decision with an audit; no rule evaluator exists."""
        validate_id(job_id, IdKind.JOB)
        if status not in QUALIFICATION_STATUSES or expected_status not in QUALIFICATION_STATUSES:
            raise ValueError("Invalid qualification status")
        with self.session.begin_nested():
            record = self.get(job_id)
            if record is None:
                raise RecordNotFoundError("Job does not exist")
            changed_id = self.session.scalar(
                update(Job)
                .where(Job.job_id == job_id, Job.qualification_status == expected_status)
                .values(qualification_status=status, updated_at=self.timestamp())
                .returning(Job.job_id)
                .execution_options(synchronize_session=False)
            )
            if changed_id is None:
                raise ConcurrentUpdateError("Job qualification status changed")
            if status != expected_status:
                ActivityLogRepository(self.session, self.clock, self.ids).append(
                    ActivityEvent(
                        "job_qualification_status_changed",
                        job_id=job_id,
                        old_state=expected_status,
                        new_state=status,
                    )
                )
            self.session.refresh(record)
        return record
