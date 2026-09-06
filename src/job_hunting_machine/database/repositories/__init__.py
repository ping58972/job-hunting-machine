"""Typed, transaction-scoped persistence APIs for Architecture v2 Phase 1."""

from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.applications import (
    ApplicationCreate,
    ApplicationCreation,
    ApplicationRepository,
)
from job_hunting_machine.database.repositories.approvals import ApprovalCreate, ApprovalRepository
from job_hunting_machine.database.repositories.artifacts import ArtifactCreate, ArtifactRepository
from job_hunting_machine.database.repositories.base import (
    ConcurrentUpdateError,
    RecordNotFoundError,
    ReplayConflictError,
)
from job_hunting_machine.database.repositories.forms import (
    AnswerUpsert,
    BrowserSessionRepository,
    FormAnswerRepository,
    FormInformationRepository,
    InformationUpsert,
    SessionCheckpoint,
)
from job_hunting_machine.database.repositories.jobs import JobCreate, JobRepository
from job_hunting_machine.database.repositories.knowledge import (
    CandidateFactRepository,
    CatalogRepository,
)
from job_hunting_machine.database.repositories.outreach import (
    ContactRepository,
    ContactUpsert,
    OutreachCreate,
    OutreachDraftRepository,
)
from job_hunting_machine.database.repositories.tasks import TaskCreate, TaskRepository

__all__ = [
    "ActivityEvent",
    "ActivityLogRepository",
    "AnswerUpsert",
    "ApplicationCreate",
    "ApplicationCreation",
    "ApplicationRepository",
    "ApprovalCreate",
    "ApprovalRepository",
    "ArtifactCreate",
    "ArtifactRepository",
    "BrowserSessionRepository",
    "CandidateFactRepository",
    "CatalogRepository",
    "ConcurrentUpdateError",
    "ContactRepository",
    "ContactUpsert",
    "FormAnswerRepository",
    "FormInformationRepository",
    "InformationUpsert",
    "JobCreate",
    "JobRepository",
    "OutreachCreate",
    "OutreachDraftRepository",
    "RecordNotFoundError",
    "ReplayConflictError",
    "SessionCheckpoint",
    "TaskCreate",
    "TaskRepository",
]
