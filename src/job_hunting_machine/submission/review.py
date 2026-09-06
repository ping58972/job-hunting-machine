"""Immutable, canonical application review snapshots and approval requests."""

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_hunting_machine.browser.artifacts import select_artifact
from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import (
    ApplicationDetails,
    ApplicationPipeline,
    Artifact,
    FormAnswer,
    Job,
)
from job_hunting_machine.database.repositories import (
    ApprovalCreate,
    ApprovalRepository,
    TaskCreate,
)
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.orchestration.checkpoints import run_blocking
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.orchestration.worker import Worker
from job_hunting_machine.orchestration.workflows import fake_workflow
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard
from job_hunting_machine.slack.control import SlackControlPlane


class ReviewError(RuntimeError):
    """Review data is incomplete, changed, or cannot be made immutable."""


def _paired_tex(
    session: Session,
    application_id: str,
    pdf: Artifact,
    artifact_type: str,
) -> Artifact:
    matches = list(
        session.scalars(
            select(Artifact).where(
                Artifact.application_id == application_id,
                Artifact.task_id == pdf.task_id,
                Artifact.version == pdf.version,
                Artifact.artifact_type == artifact_type,
            )
        )
    )
    if len(matches) != 1:
        raise ReviewError(f"review_{artifact_type.casefold()}_missing_or_ambiguous")
    return matches[0]


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    application_id: str
    task_id: str
    approval_id: str
    path: str
    sha256: str
    payload: dict[str, object]


def review_payload(
    session: Session,
    application_id: str,
    *,
    created_at: str,
) -> dict[str, object]:
    validate_id(application_id, IdKind.APPLICATION)
    app = session.get(ApplicationPipeline, application_id)
    details = session.get(ApplicationDetails, application_id)
    if not app or not details:
        raise ReviewError("review_application_missing")
    job = session.get(Job, app.job_id)
    if not job:
        raise ReviewError("review_job_missing")
    resume = select_artifact(session, application_id, details.resume_artifact_id, "RESUME_PDF")
    resume_row = session.get(Artifact, resume.artifact_id)
    assert resume_row is not None
    resume_tex_row = _paired_tex(session, application_id, resume_row, "RESUME_TEX")
    resume_tex = select_artifact(session, application_id, resume_tex_row.artifact_id, "RESUME_TEX")
    cover = (
        select_artifact(
            session,
            application_id,
            details.cover_letter_artifact_id,
            "COVER_LETTER_PDF",
        )
        if details.cover_letter_artifact_id
        else None
    )
    cover_tex = None
    if cover is not None:
        cover_row = session.get(Artifact, cover.artifact_id)
        assert cover_row is not None
        cover_tex_row = _paired_tex(session, application_id, cover_row, "COVER_LETTER_TEX")
        cover_tex = select_artifact(
            session, application_id, cover_tex_row.artifact_id, "COVER_LETTER_TEX"
        )
    answers: list[dict[str, object]] = []
    for answer in session.scalars(
        select(FormAnswer)
        .where(FormAnswer.application_id == application_id)
        .order_by(FormAnswer.page_key, FormAnswer.field_key)
    ):
        if answer.answer_status != "VALIDATED" or answer.answer_json is None:
            raise ReviewError("review_contains_unvalidated_answer")
        answers.append(
            {
                "page_key": answer.page_key,
                "field_key": answer.field_key,
                "question_text": answer.question_text,
                "answer": json.loads(answer.answer_json),
                "source_type": answer.answer_source_type,
                "source_reference": answer.answer_source_reference,
                "verified_at": answer.last_verified_at,
            }
        )
    other_uploads: list[dict[str, object]] = []
    for artifact in session.scalars(
        select(Artifact)
        .where(
            Artifact.application_id == application_id,
            Artifact.artifact_type == "TRANSCRIPT",
        )
        .order_by(Artifact.artifact_id)
    ):
        selected = select_artifact(session, application_id, artifact.artifact_id, "TRANSCRIPT")
        other_uploads.append(
            {
                "artifact_id": selected.artifact_id,
                "type": selected.artifact_type,
                "filename": selected.path.name,
                "sha256": selected.sha256,
            }
        )
    return {
        "application_id": application_id,
        "job": {
            "job_id": job.job_id,
            "company_name": details.company_name,
            "job_title": details.job_title,
            "job_url": details.job_url,
            "location_text": details.location_text,
        },
        "answers": answers,
        "resume": {
            "tex_artifact_id": resume_tex.artifact_id,
            "tex_filename": resume_tex.path.name,
            "tex_sha256": resume_tex.sha256,
            "pdf_artifact_id": resume.artifact_id,
            "pdf_filename": resume.path.name,
            "pdf_sha256": resume.sha256,
        },
        "cover_letter": (
            {
                "tex_artifact_id": cover_tex.artifact_id,
                "tex_filename": cover_tex.path.name,
                "tex_sha256": cover_tex.sha256,
                "pdf_artifact_id": cover.artifact_id,
                "pdf_filename": cover.path.name,
                "pdf_sha256": cover.sha256,
            }
            if cover and cover_tex
            else None
        ),
        "other_uploads": other_uploads,
        "application_url": details.application_url or details.job_url,
        "created_at": created_at,
    }


def read_review(path: str, sha256: str) -> dict[str, object]:
    checked = PathGuard().validate_write(PROJECT_ROOT / path)
    try:
        data = checked.read_bytes()
    except OSError:
        raise ReviewError("review_payload_unavailable") from None
    if len(data) > 5_000_000 or hashlib.sha256(data).hexdigest() != sha256:
        raise ReviewError("review_payload_changed")
    try:
        value = json.loads(data)
    except (ValueError, TypeError):
        raise ReviewError("review_payload_invalid") from None
    if not isinstance(value, dict) or canonical_json(value) != data:
        raise ReviewError("review_payload_not_canonical")
    return value


class ReviewService:
    def __init__(
        self,
        queue: QueueService,
        *,
        slack: SlackControlPlane | None = None,
        approval_ttl_seconds: int = 86400,
    ) -> None:
        if not 60 <= approval_ttl_seconds <= 86400:
            raise ValueError("invalid_submission_approval_ttl")
        self.queue = queue
        self.slack = slack
        self.approval_ttl_seconds = approval_ttl_seconds

    def prepare(self, lease: Lease) -> ReviewRecord:
        if lease.task_type != "CREATE_REVIEW":
            raise ReviewError("review_task_type_invalid")
        application_id = lease.payload.get("application_id")
        if not isinstance(application_id, str):
            raise ReviewError("review_task_application_missing")
        approval_id = self.queue.ids.new(IdKind.APPROVAL)
        submission_task_id = self.queue.ids.new(IdKind.TASK)
        review_id = self.queue.ids.generate_ulid()
        stamp = format_utc(self.queue.clock.now())
        with self.queue.fence(lease) as session:
            task = self.queue._get(session, lease.task_id)
            app = session.get(ApplicationPipeline, application_id)
            if (
                task.application_id != application_id
                or not app
                or app.current_task_id != lease.task_id
                or app.pipeline_stage != "REVIEW"
                or app.application_status != "READY_TO_REVIEW"
            ):
                raise ReviewError("review_application_state_invalid")
            payload = review_payload(session, application_id, created_at=stamp)
            content = canonical_json(payload)
            sha256 = hashlib.sha256(content).hexdigest()
            directory = PathGuard().mkdir(
                PROJECT_ROOT / "applications" / application_id / "review" / review_id,
                parents=True,
                exist_ok=True,
            )
            path = directory / "review.json"
            if path.exists() and path.read_bytes() != content:
                raise ReviewError("review_path_content_conflict")
            PathGuard().write_bytes(path, content)
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            interrupt_id = self.queue.ids.generate_ulid()
            submission = self.queue.enqueue_waiting_in_transaction(
                session,
                TaskCreate(
                    "SUBMIT_APPLICATION",
                    application_id=application_id,
                    job_id=app.job_id,
                    parent_task_id=lease.task_id,
                    dedupe_key=f"submit_task:{application_id}:{sha256}",
                    payload={
                        "application_id": application_id,
                        "approval_id": approval_id,
                        "review_path": relative,
                        "review_sha256": sha256,
                    },
                    task_id=submission_task_id,
                    max_attempts=3,
                ),
                {
                    "interrupts": {
                        interrupt_id: {
                            "kind": "SUBMISSION_APPROVAL",
                            "application_id": application_id,
                            "approval_id": approval_id,
                            "review_sha256": sha256,
                        }
                    }
                },
            )
            approval = ApprovalRepository(session, self.queue.clock, self.queue.ids).create(
                ApprovalCreate(
                    "SUBMIT_APPLICATION",
                    relative,
                    sha256,
                    application_id,
                    submission_task_id,
                    format_utc(
                        self.queue.clock.now() + timedelta(seconds=self.approval_ttl_seconds)
                    ),
                    approval_id,
                )
            )
            app.current_task_id = submission.task_id
            app.updated_at = stamp
            self.queue.complete_in_transaction(
                session,
                lease,
                {"review_path": relative, "review_sha256": sha256},
            )
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "submission_review_created",
                    task_id=submission.task_id,
                    application_id=application_id,
                    new_state="PENDING",
                    metadata={"approval_id": approval.approval_id, "review_sha256": sha256},
                )
            )
        if self.slack:
            self.slack.request_approval(approval_id)
        return ReviewRecord(
            application_id,
            submission_task_id,
            approval_id,
            relative,
            sha256,
            payload,
        )


class ReviewWorker(Worker):
    def __init__(
        self,
        queue: QueueService,
        *,
        slack: SlackControlPlane | None = None,
        checkpoint_path: Path | None = None,
    ) -> None:
        super().__init__(
            queue,
            {"CREATE_REVIEW": fake_workflow()},
            checkpoint_path=checkpoint_path
            or queue.database.path.parent / "langgraph-checkpoints.db",
        )
        self.service = ReviewService(queue, slack=slack)

    async def _execute(self, lease: Lease) -> None:
        await run_blocking(self.service.prepare, lease)
