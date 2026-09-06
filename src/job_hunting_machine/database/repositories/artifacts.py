"""Persist artifact metadata; artifact generation and content validation are later."""

import re
from dataclasses import dataclass

from job_hunting_machine.database.models import Artifact
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import ReplayConflictError, Repository
from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_TYPES = frozenset(
    {
        "RESUME_TEX",
        "RESUME_PDF",
        "COVER_LETTER_TEX",
        "COVER_LETTER_PDF",
        "TRANSCRIPT",
        "SCREENSHOT",
        "JOB_SNAPSHOT",
        "OTHER",
    }
)


def local_record_path(path: str) -> str:
    """Validate only stored path metadata; this function does not write a file."""
    if not path.strip():
        raise ValueError("A non-empty artifact path is required")
    return PathGuard().validate_write(path).relative_to(PROJECT_ROOT).as_posix()


def validate_sha256(value: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("A lowercase SHA-256 digest is required")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactCreate:
    artifact_type: str
    path: str
    sha256: str
    mime_type: str
    application_id: str | None = None
    task_id: str | None = None
    version: int = 1
    artifact_id: str | None = None


class ArtifactRepository(Repository):
    def get(self, artifact_id: str) -> Artifact | None:
        return self.session.get(Artifact, validate_id(artifact_id, IdKind.ARTIFACT))

    def create(self, data: ArtifactCreate) -> Artifact:
        if data.artifact_type not in _ARTIFACT_TYPES:
            raise ValueError("Invalid artifact type")
        if data.version < 1:
            raise ValueError("Artifact version must be positive")
        path = local_record_path(data.path)
        validate_sha256(data.sha256)
        for value, kind in (
            (data.artifact_id, IdKind.ARTIFACT),
            (data.application_id, IdKind.APPLICATION),
            (data.task_id, IdKind.TASK),
        ):
            if value is not None:
                validate_id(value, kind)
        existing = self.get(data.artifact_id) if data.artifact_id is not None else None
        if existing is not None:
            if (
                existing.artifact_type != data.artifact_type
                or existing.path != path
                or existing.sha256 != data.sha256
                or existing.mime_type != data.mime_type
                or existing.application_id != data.application_id
                or existing.task_id != data.task_id
                or existing.version != data.version
            ):
                raise ReplayConflictError("Artifact ID was reused for different metadata")
            return existing
        with self.session.begin_nested():
            record = Artifact(
                artifact_id=data.artifact_id or self.ids.new(IdKind.ARTIFACT),
                application_id=data.application_id,
                task_id=data.task_id,
                artifact_type=data.artifact_type,
                path=path,
                sha256=data.sha256,
                mime_type=data.mime_type,
                version=data.version,
                approved_for_submission=False,
                created_at=self.timestamp(),
            )
            self.session.add(record)
            self.session.flush()
            ActivityLogRepository(self.session, self.clock, self.ids).append(
                ActivityEvent(
                    "artifact_created",
                    application_id=data.application_id,
                    task_id=data.task_id,
                    metadata={"artifact_id": record.artifact_id},
                )
            )
        return record
