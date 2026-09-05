"""Atomic qualified-application creation without running a qualification agent."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from job_hunting_machine.database.models import AgentTask, ApplicationDetails, ApplicationPipeline
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import (
    RecordNotFoundError,
    ReplayConflictError,
    Repository,
    required_text,
)
from job_hunting_machine.database.repositories.jobs import JobRepository
from job_hunting_machine.database.repositories.tasks import TaskCreate, TaskRepository
from job_hunting_machine.ids import IdKind, validate_id


@dataclass(frozen=True, slots=True)
class ApplicationCreate:
    company_name: str
    job_title: str
    job_url: str
    application_url: str | None = None
    ats_type: str | None = None
    location_text: str | None = None
    employment_type: str | None = None
    application_id: str | None = None
    resume_task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApplicationCreation:
    pipeline: ApplicationPipeline
    details: ApplicationDetails
    task: AgentTask
    created: bool

    @property
    def application_id(self) -> str:
        return self.pipeline.application_id

    @property
    def task_id(self) -> str:
        return self.task.task_id


class ApplicationRepository(Repository):
    def get(self, application_id: str) -> ApplicationPipeline | None:
        return self.session.get(
            ApplicationPipeline, validate_id(application_id, IdKind.APPLICATION)
        )

    def get_by_job_id(self, job_id: str) -> ApplicationPipeline | None:
        validate_id(job_id, IdKind.JOB)
        return self.session.scalar(
            select(ApplicationPipeline).where(ApplicationPipeline.job_id == job_id)
        )

    def get_details(self, application_id: str) -> ApplicationDetails | None:
        return self.session.get(ApplicationDetails, validate_id(application_id, IdKind.APPLICATION))

    def _existing(
        self, pipeline: ApplicationPipeline, data: ApplicationCreate
    ) -> ApplicationCreation:
        details = self.get_details(pipeline.application_id)
        task = TaskRepository(self.session, self.clock, self.ids).get_by_dedupe_key(
            f"build_resume:{pipeline.application_id}"
        )
        if details is None or task is None:
            raise ReplayConflictError("Existing application creation is incomplete")
        if (
            details.company_name != data.company_name
            or details.job_title != data.job_title
            or details.job_url != data.job_url
            or details.application_url != data.application_url
            or details.ats_type != data.ats_type
            or details.location_text != data.location_text
            or details.employment_type != data.employment_type
            or (data.application_id is not None and pipeline.application_id != data.application_id)
            or (data.resume_task_id is not None and task.task_id != data.resume_task_id)
            or task.application_id != pipeline.application_id
            or task.job_id != pipeline.job_id
            or task.task_type != "BUILD_RESUME"
        ):
            raise ReplayConflictError("Application replay data differs from its persisted identity")
        return ApplicationCreation(pipeline, details, task, created=False)

    def create_from_passed_job(self, job_id: str, data: ApplicationCreate) -> ApplicationCreation:
        """Create pipeline, details, ready resume task and audits in one savepoint.

        The enclosing transaction controls commit. A failed operation rolls back
        its entire savepoint, even when the caller catches the exception.
        """
        validate_id(job_id, IdKind.JOB)
        required_text(data.company_name)
        required_text(data.job_title)
        required_text(data.job_url)
        if data.application_id is not None:
            validate_id(data.application_id, IdKind.APPLICATION)
        if data.resume_task_id is not None:
            validate_id(data.resume_task_id, IdKind.TASK)
        job = JobRepository(self.session, self.clock, self.ids).get(job_id)
        if job is None:
            raise RecordNotFoundError("Job does not exist")
        if job.qualification_status != "PASSED":
            raise ValueError("Application creation requires a persisted PASSED job")
        if data.job_url not in {job.original_url, job.canonical_url}:
            raise ValueError("Application job URL must match the persisted passed job")
        existing = self.get_by_job_id(job_id)
        if existing is not None:
            return self._existing(existing, data)
        try:
            with self.session.begin_nested():
                application_id = data.application_id or self.ids.new(IdKind.APPLICATION)
                now = self.timestamp()
                pipeline = ApplicationPipeline(
                    application_id=application_id,
                    job_id=job_id,
                    pipeline_stage="RESUME",
                    application_status="QUALIFIED",
                    priority=100,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(pipeline)
                self.session.flush()
                details = ApplicationDetails(
                    application_id=application_id,
                    company_name=data.company_name,
                    job_title=data.job_title,
                    job_url=data.job_url,
                    application_url=data.application_url,
                    ats_type=data.ats_type,
                    location_text=data.location_text,
                    employment_type=data.employment_type,
                    application_status="QUALIFIED",
                    referral_contact_status="NOT_STARTED",
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(details)
                self.session.flush()
                task = TaskRepository(self.session, self.clock, self.ids).create(
                    TaskCreate(
                        task_type="BUILD_RESUME",
                        task_status="READY",
                        dedupe_key=f"build_resume:{application_id}",
                        application_id=application_id,
                        job_id=job_id,
                        task_id=data.resume_task_id,
                    )
                )
                pipeline.current_task_id = task.task_id
                self.session.flush()
                ActivityLogRepository(self.session, self.clock, self.ids).append(
                    ActivityEvent(
                        "application_created",
                        application_id=application_id,
                        job_id=job_id,
                        task_id=task.task_id,
                        new_state="QUALIFIED",
                        metadata={"pipeline_stage": "RESUME"},
                    )
                )
            return ApplicationCreation(pipeline, details, task, created=True)
        except IntegrityError:
            existing = self.get_by_job_id(job_id)
            if existing is None:
                raise
            return self._existing(existing, data)
