"""Exact, application-scoped artifact selection and integrity checks."""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from job_hunting_machine.database.models import ApplicationDetails, Artifact
from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


@dataclass(frozen=True, slots=True)
class SelectedArtifact:
    artifact_id: str
    artifact_type: str
    path: Path
    sha256: str
    mime_type: str
    content: bytes


def select_artifact(
    session: Session,
    application_id: str,
    artifact_id: str | None,
    expected_type: str,
) -> SelectedArtifact:
    validate_id(application_id, IdKind.APPLICATION)
    if artifact_id is None:
        raise ValueError(f"missing_{expected_type.lower()}_artifact")
    row = session.get(Artifact, validate_id(artifact_id, IdKind.ARTIFACT))
    if row is None or row.application_id != application_id or row.artifact_type != expected_type:
        raise ValueError("artifact_application_or_type_mismatch")
    path = PathGuard().validate_write(PROJECT_ROOT / row.path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > 20_000_000
            ):
                raise ValueError("artifact_file_unavailable")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read()
        finally:
            os.close(descriptor)
    except OSError:
        raise ValueError("artifact_file_unavailable") from None
    if hashlib.sha256(content).hexdigest() != row.sha256:
        raise ValueError("artifact_hash_mismatch")
    return SelectedArtifact(
        row.artifact_id,
        row.artifact_type,
        path,
        row.sha256,
        row.mime_type or "application/octet-stream",
        content,
    )


def application_artifacts(
    session: Session, application_id: str, *, transcript_id: str | None = None
) -> dict[str, SelectedArtifact]:
    details = session.get(ApplicationDetails, validate_id(application_id, IdKind.APPLICATION))
    if details is None:
        raise ValueError("application_details_missing")
    selected = {
        "resume": select_artifact(session, application_id, details.resume_artifact_id, "RESUME_PDF")
    }
    if details.cover_letter_artifact_id:
        selected["cover_letter"] = select_artifact(
            session,
            application_id,
            details.cover_letter_artifact_id,
            "COVER_LETTER_PDF",
        )
    if transcript_id:
        selected["transcript"] = select_artifact(
            session, application_id, transcript_id, "TRANSCRIPT"
        )
    return selected
