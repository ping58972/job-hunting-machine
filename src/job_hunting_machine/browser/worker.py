"""Lease-aware Phase 8 form preparation worker. Final submission is absent."""

import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from job_hunting_machine.browser.actions import ExternalActionService, canonical_bytes, value_hash
from job_hunting_machine.browser.artifacts import SelectedArtifact, application_artifacts
from job_hunting_machine.browser.config import FormPolicy, load_form_policy
from job_hunting_machine.browser.detector import adapter_for
from job_hunting_machine.browser.manager import BrowserManager, BrowserSessionHandle
from job_hunting_machine.browser.resolver import CanonicalFieldResolver, ResolvedField, normalize
from job_hunting_machine.browser.types import BaseAdapter, PageSnapshot, SemanticField, UploadFile
from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import (
    ApplicationDetails,
    ApplicationPipeline,
    Approval,
    CandidateFact,
    FormAnswer,
    FormInformation,
)
from job_hunting_machine.database.repositories import (
    AnswerUpsert,
    ApprovalCreate,
    ApprovalRepository,
    BrowserSessionRepository,
    FormAnswerRepository,
    FormInformationRepository,
    SessionCheckpoint,
)
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.ids import IdKind
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.orchestration.worker import Worker
from job_hunting_machine.orchestration.workflows import fake_workflow
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard
from job_hunting_machine.slack.control import SlackControlPlane
from job_hunting_machine.slack.messages import Question


class FormPreparationError(RuntimeError):
    """A safe, static form-preparation failure."""


@dataclass(frozen=True, slots=True)
class PlannedField:
    field: SemanticField
    value: object
    source_type: str
    source_reference: str
    canonical_key: str | None
    artifact: SelectedArtifact | None = None


class FormWorker(Worker):
    """Prepare forms through review and stop before every final-submit control."""

    def __init__(
        self,
        queue: QueueService,
        manager: BrowserManager,
        *,
        resolver: CanonicalFieldResolver | None = None,
        slack: SlackControlPlane | None = None,
        policy: FormPolicy | None = None,
        checkpoint_path: Path | None = None,
    ) -> None:
        super().__init__(
            queue,
            {"FORM_PROCESS": fake_workflow()},
            checkpoint_path=checkpoint_path
            or queue.database.path.parent / "langgraph-checkpoints.db",
        )
        self.manager = manager
        self.runtime_mode = manager.settings.runtime_mode
        self.resolver = resolver or CanonicalFieldResolver()
        self.slack = slack
        self.policy = policy or load_form_policy()
        self.actions = ExternalActionService(queue)

    async def startup(self) -> int:
        recovered = await super().startup()
        return recovered + self.actions.recover()

    def request_stop(self) -> None:
        super().request_stop()

    async def close(self) -> None:
        await self.manager.close()

    def _application(self, lease: Lease) -> tuple[str, str]:
        with self.queue.fence(lease) as session:
            task = self.queue._get(session, lease.task_id)
            if not task.application_id or task.application_id != lease.payload.get(
                "application_id", task.application_id
            ):
                raise FormPreparationError("form_task_application_mismatch")
            app = session.get(ApplicationPipeline, task.application_id)
            details = session.get(ApplicationDetails, task.application_id)
            if not app or not details or app.current_task_id != lease.task_id:
                raise FormPreparationError("form_task_not_current")
            if app.application_status == "READY_TO_REVIEW":
                return app.application_id, details.application_url or details.job_url
            if app.pipeline_stage != "FORM" or app.application_status not in {
                "FORM_READY",
                "FORM_IN_PROGRESS",
                "WAITING_USER_INPUT",
            }:
                raise FormPreparationError("application_not_form_ready")
            return app.application_id, details.application_url or details.job_url

    def _artifacts(self, lease: Lease, application_id: str) -> dict[str, SelectedArtifact]:
        transcript = lease.payload.get("transcript_artifact_id")
        if transcript is not None and not isinstance(transcript, str):
            raise FormPreparationError("invalid_transcript_artifact_id")
        with self.queue.fence(lease) as session:
            return application_artifacts(session, application_id, transcript_id=transcript)

    def _checkpoint(
        self,
        lease: Lease,
        handle: BrowserSessionHandle,
        snapshot: PageSnapshot,
        ats_type: str,
        *,
        status: str = "ACTIVE",
    ) -> None:
        with self.queue.fence(lease) as session:
            repository = BrowserSessionRepository(session, self.queue.clock, self.queue.ids)
            repository.checkpoint(
                SessionCheckpoint(
                    handle.application_id,
                    ats_type,
                    str(handle.storage_state_path),
                    snapshot.url,
                    snapshot.page_key,
                    status,
                )
            )

    @staticmethod
    def _file_kind(field: SemanticField) -> str | None:
        label = normalize(f"{field.label} {field.name or ''} {field.field_key}")
        if "resume" in label or "cv" in label.split():
            return "resume"
        if "cover letter" in label:
            return "cover_letter"
        if "transcript" in label:
            return "transcript"
        return None

    async def _resolve(
        self,
        lease: Lease,
        application_id: str,
        snapshot: PageSnapshot,
        artifacts: dict[str, SelectedArtifact],
        memory: dict[str, object],
    ) -> tuple[list[PlannedField], ResolvedField | None]:
        planned: list[PlannedField] = []
        for field in snapshot.fields:
            if field.control == "file":
                kind = self._file_kind(field)
                artifact = artifacts.get(kind or "")
                if kind is None or artifact is None:
                    unresolved = ResolvedField(
                        field,
                        "NEEDS_USER",
                        reason="missing_or_unknown_file_artifact",
                    )
                    self._persist_answer(lease, application_id, snapshot.page_key, unresolved)
                    return planned, unresolved
                planned.append(
                    PlannedField(
                        field,
                        artifact.path.name,
                        "FILE",
                        artifact.artifact_id,
                        None,
                        artifact,
                    )
                )
                self._persist_answer(
                    lease,
                    application_id,
                    snapshot.page_key,
                    ResolvedField(
                        field,
                        "RESOLVED",
                        artifact.path.name,
                        "FILE",
                        artifact.artifact_id,
                        confidence=1.0,
                    ),
                )
                continue
            resolved = self._user_override(memory, snapshot, field)
            if resolved is None:
                with self.queue.fence(lease) as session:
                    values = self.resolver.values(session)
                resolved = await self.resolver.resolve_values(lease.task_id, field, values)
            self._persist_answer(lease, application_id, snapshot.page_key, resolved)
            if resolved.status != "RESOLVED":
                return planned, resolved
            assert resolved.source_type and resolved.source_reference
            planned.append(
                PlannedField(
                    field,
                    resolved.value,
                    resolved.source_type,
                    resolved.source_reference,
                    resolved.canonical_key,
                )
            )
        return planned, None

    def _persist_answer(
        self, lease: Lease, application_id: str, page_key: str, resolved: ResolvedField
    ) -> None:
        status = "FILLED" if resolved.status == "RESOLVED" else "NEEDS_USER"
        with self.queue.fence(lease) as session:
            FormAnswerRepository(session, self.queue.clock, self.queue.ids).upsert(
                AnswerUpsert(
                    application_id,
                    page_key,
                    resolved.field.field_key,
                    resolved.field.label,
                    resolved.value,
                    resolved.source_type,  # type: ignore[arg-type]
                    resolved.source_reference,
                    status,  # type: ignore[arg-type]
                    resolved.confidence,
                )
            )

    def _handle_resume(self, lease: Lease, memory: dict[str, object]) -> None:
        reply = memory.get("resume")
        interrupts = memory.get("interrupts")
        if not isinstance(reply, dict) or not isinstance(interrupts, dict):
            return
        interrupt_id = reply.get("interrupt_id")
        item = interrupts.get(interrupt_id) if isinstance(interrupt_id, str) else None
        if not isinstance(item, dict):
            return
        value = reply.get("value")
        if item.get("kind") == "FORM_FIELD":
            if not isinstance(value, dict) or not isinstance(value.get("answer"), str):
                raise FormPreparationError("invalid_form_answer_resume")
            application_id = str(item["application_id"])
            reference = (
                f"slack:{value['event_id']}:{value['user_id']}"
                if isinstance(value.get("event_id"), str) and isinstance(value.get("user_id"), str)
                else f"local:{lease.task_id}"
            )
            with self.queue.fence(lease) as session:
                FormAnswerRepository(session, self.queue.clock, self.queue.ids).upsert(
                    AnswerUpsert(
                        application_id,
                        str(item["page_key"]),
                        str(item["field_key"]),
                        str(item["question_text"]),
                        value["answer"],
                        "USER",
                        reference,
                        "FILLED",
                        1.0,
                    )
                )
                canonical_key = item.get("canonical_key")
                if isinstance(canonical_key, str):
                    with suppress(ValueError):
                        FormInformationRepository(
                            session, self.queue.clock, self.queue.ids
                        ).save_authorized_user_answer(
                            canonical_key, value["answer"], user_reference=reference
                        )
                app = session.get(ApplicationPipeline, application_id)
                details = session.get(ApplicationDetails, application_id)
                assert app and details
                if app.application_status == "WAITING_USER_INPUT":
                    app.application_status = details.application_status = "FORM_IN_PROGRESS"
                    app.updated_at = details.updated_at = format_utc(self.queue.clock.now())
                    ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                        ActivityEvent(
                            "form_user_answer_recorded",
                            task_id=lease.task_id,
                            application_id=application_id,
                            old_state="WAITING_USER_INPUT",
                            new_state="FORM_IN_PROGRESS",
                        )
                    )
            memory.setdefault("user_answers", {})
            answers = memory["user_answers"]
            if isinstance(answers, dict):
                answers[f"{item['page_key']}:{item['field_key']}"] = {
                    "answer": value["answer"],
                    "source_reference": reference,
                }
        memory.pop("resume", None)
        memory["interrupts"] = {}
        self.queue.save_memory(lease, memory)

    def _user_override(
        self, memory: dict[str, object], snapshot: PageSnapshot, field: SemanticField
    ) -> ResolvedField | None:
        answers = memory.get("user_answers")
        if not isinstance(answers, dict):
            return None
        value = answers.get(f"{snapshot.page_key}:{field.field_key}")
        if not isinstance(value, dict) or "answer" not in value:
            return None
        return ResolvedField(
            field,
            "RESOLVED",
            value["answer"],
            "USER",
            str(value.get("source_reference", "user")),
            confidence=1.0,
        )

    async def _wait(
        self,
        lease: Lease,
        memory: dict[str, object],
        payload: dict[str, object],
        *,
        question: bool = False,
        approval_id: str | None = None,
    ) -> None:
        if payload.get("kind") in {"FORM_FIELD", "BROWSER_CHALLENGE", "UNSUPPORTED_ATS"}:
            application_id = payload.get("application_id")
            if isinstance(application_id, str):
                with self.queue.fence(lease) as session:
                    app = session.get(ApplicationPipeline, application_id)
                    details = session.get(ApplicationDetails, application_id)
                    assert app and details
                    old = app.application_status
                    app.application_status = details.application_status = "WAITING_USER_INPUT"
                    app.updated_at = details.updated_at = format_utc(self.queue.clock.now())
                    ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                        ActivityEvent(
                            "form_waiting_for_user",
                            task_id=lease.task_id,
                            application_id=application_id,
                            old_state=old,
                            new_state="WAITING_USER_INPUT",
                            metadata={"reason": payload["kind"]},
                        )
                    )
        interrupt_id = self.queue.ids.generate_ulid()
        memory["interrupts"] = {interrupt_id: payload}
        memory.pop("resume", None)
        self.queue.complete(lease, memory, waiting=True)
        if self.slack and question:
            self.slack.ask_missing(lease.task_id, interrupt_id, Question.MISSING_INFORMATION)
        if self.slack and approval_id:
            self.slack.request_approval(approval_id)

    def _plan(
        self,
        lease: Lease,
        application_id: str,
        snapshot: PageSnapshot,
        ats: str,
        fields: list[PlannedField],
    ) -> dict[str, object]:
        return {
            "operation": "PREPARE_APPLICATION",
            "task_id": lease.task_id,
            "application_id": application_id,
            "ats": ats,
            "url": snapshot.url,
            "page_key": snapshot.page_key,
            "fields": [
                {
                    "field_key": item.field.field_key,
                    "value": item.value,
                    "source_type": item.source_type,
                    "source_reference": item.source_reference,
                    "artifact_id": item.artifact.artifact_id if item.artifact else None,
                    "artifact_sha256": item.artifact.sha256 if item.artifact else None,
                }
                for item in fields
            ],
        }

    def _approval(
        self,
        lease: Lease,
        application_id: str,
        memory: dict[str, object],
        plan: dict[str, object],
    ) -> tuple[Approval, bool]:
        content = canonical_bytes(plan)
        sha = hashlib.sha256(content).hexdigest()
        previous = memory.get("prepare_approval")
        with self.queue.fence(lease) as session:
            if isinstance(previous, dict):
                old = session.get(Approval, previous.get("approval_id"))
                if old and previous.get("plan_hash") == sha:
                    return old, False
                if old and old.approval_status in {"PENDING", "APPROVED"}:
                    old.approval_status = "REVOKED"
                    ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                        ActivityEvent(
                            "prepare_approval_revoked",
                            task_id=lease.task_id,
                            application_id=application_id,
                            old_state=previous.get("plan_hash"),
                            new_state=sha,
                            metadata={"approval_id": old.approval_id},
                        )
                    )
            approval_id = self.queue.ids.new(IdKind.APPROVAL)
            directory = PathGuard().mkdir(
                PROJECT_ROOT / "applications" / application_id / "approvals",
                parents=True,
                exist_ok=True,
            )
            path = PathGuard().write_bytes(directory / f"{approval_id}.json", content)
            row = ApprovalRepository(session, self.queue.clock, self.queue.ids).create(
                ApprovalCreate(
                    "PREPARE_APPLICATION",
                    str(path),
                    sha,
                    application_id,
                    lease.task_id,
                    format_utc(
                        self.queue.clock.now() + timedelta(seconds=self.policy.approval_ttl_seconds)
                    ),
                    approval_id,
                )
            )
            memory["prepare_approval"] = {"approval_id": row.approval_id, "plan_hash": sha}
            return row, True

    def _mark_started(self, lease: Lease, application_id: str) -> None:
        with self.queue.fence(lease) as session:
            app = session.get(ApplicationPipeline, application_id)
            details = session.get(ApplicationDetails, application_id)
            assert app and details
            if app.application_status == "FORM_READY":
                app.application_status = details.application_status = "FORM_IN_PROGRESS"
                app.updated_at = details.updated_at = format_utc(self.queue.clock.now())
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "form_preparation_started",
                        task_id=lease.task_id,
                        application_id=application_id,
                        old_state="FORM_READY",
                        new_state="FORM_IN_PROGRESS",
                    )
                )

    def _revalidate_fields(
        self,
        lease: Lease,
        application_id: str,
        snapshot: PageSnapshot,
        fields: list[PlannedField],
    ) -> None:
        with self.queue.fence(lease) as session:
            for item in fields:
                if item.source_type == "FILE":
                    if not item.artifact or hashlib.sha256(item.artifact.content).hexdigest() != (
                        item.artifact.sha256
                    ):
                        raise FormPreparationError("artifact_changed_before_browser_action")
                    continue
                if item.source_type == "CANONICAL_INFO":
                    row = session.get(FormInformation, item.source_reference)
                    if (
                        not row
                        or not row.verified
                        or row.auto_fill_policy == "MANUAL_ONLY"
                        or json.loads(row.value_json) != item.value
                    ):
                        raise FormPreparationError("canonical_answer_changed_before_action")
                elif item.source_type == "CANDIDATE_FACT":
                    fact = session.get(CandidateFact, item.source_reference)
                    if (
                        not fact
                        or fact.verification_status != "VERIFIED"
                        or json.loads(fact.value_json) != item.value
                    ):
                        raise FormPreparationError("candidate_fact_changed_before_action")
                elif item.source_type == "USER":
                    answer = session.scalar(
                        select(FormAnswer).where(
                            FormAnswer.application_id == application_id,
                            FormAnswer.page_key == snapshot.page_key,
                            FormAnswer.field_key == item.field.field_key,
                        )
                    )
                    if (
                        not answer
                        or answer.answer_source_reference != item.source_reference
                        or json.loads(answer.answer_json or "null") != item.value
                    ):
                        raise FormPreparationError("user_answer_changed_before_action")
                else:
                    raise FormPreparationError("unapproved_answer_source")

    async def _apply(
        self,
        lease: Lease,
        application_id: str,
        handle: BrowserSessionHandle,
        adapter: BaseAdapter,
        snapshot: PageSnapshot,
        fields: list[PlannedField],
        approval: Approval,
        plan_hash: str,
    ) -> None:
        for item in fields:

            async def confirm(item: PlannedField = item) -> bool:
                current = await adapter.inspect(handle.page)
                found = next(
                    (field for field in current.fields if field.field_key == item.field.field_key),
                    None,
                )
                if found is None:
                    return False
                expected = item.artifact.path.name if item.artifact else item.value
                return str(found.current_value) == str(expected)

            request = {
                "field_key": item.field.field_key,
                "value_hash": value_hash(item.value),
                "artifact_id": item.artifact.artifact_id if item.artifact else None,
                "artifact_sha256": item.artifact.sha256 if item.artifact else None,
            }
            key = (
                f"browser:{lease.task_id}:{snapshot.page_key}:{item.field.field_key}:"
                f"{value_hash(request)}"
            )
            if item.artifact:

                async def mutate(item: PlannedField = item) -> None:
                    assert item.artifact
                    await adapter.upload(
                        handle.page,
                        item.field,
                        UploadFile(
                            item.artifact.path.name,
                            item.artifact.mime_type,
                            item.artifact.content,
                        ),
                    )

                action_type = "BROWSER_UPLOAD"
            else:

                async def mutate(item: PlannedField = item) -> None:
                    await adapter.fill(handle.page, item.field, item.value)

                action_type = "BROWSER_FILL"
            await self.actions.execute(
                lease,
                application_id=application_id,
                approval_id=approval.approval_id,
                plan_hash=plan_hash,
                action_type=action_type,
                idempotency_key=key,
                request=request,
                mutate=mutate,
                confirm=confirm,
            )
            self._mark_started(lease, application_id)
            with self.queue.fence(lease) as session:
                FormAnswerRepository(session, self.queue.clock, self.queue.ids).upsert(
                    AnswerUpsert(
                        application_id,
                        snapshot.page_key,
                        item.field.field_key,
                        item.field.label,
                        item.value,
                        item.source_type,  # type: ignore[arg-type]
                        item.source_reference,
                        "VALIDATED",
                        1.0,
                    )
                )
            await self.manager.save_state(handle)
            self._checkpoint(lease, handle, await adapter.inspect(handle.page), adapter.kind.value)

    def _finish(self, lease: Lease, application_id: str, memory: dict[str, object]) -> None:
        with self.queue.fence(lease) as session:
            app = session.get(ApplicationPipeline, application_id)
            details = session.get(ApplicationDetails, application_id)
            assert app and details
            old = app.application_status
            app.pipeline_stage = "REVIEW"
            app.application_status = details.application_status = "READY_TO_REVIEW"
            app.updated_at = details.updated_at = format_utc(self.queue.clock.now())
            memory["interrupts"] = {}
            memory.pop("resume", None)
            self.queue.complete_in_transaction(session, lease, memory)
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "form_preparation_completed",
                    task_id=lease.task_id,
                    application_id=application_id,
                    old_state=old,
                    new_state="READY_TO_REVIEW",
                    metadata={"pipeline_stage": "REVIEW"},
                )
            )

    async def _execute(self, lease: Lease) -> None:
        memory = self.queue.memory(lease.task_id)
        self._handle_resume(lease, memory)
        application_id, initial_url = self._application(lease)
        artifacts = self._artifacts(lease, application_id)
        latest_url = initial_url
        with self.queue.database.transaction() as session:
            prior = BrowserSessionRepository(session, self.queue.clock, self.queue.ids).latest(
                application_id
            )
            if prior and prior.current_url:
                latest_url = prior.current_url
        async with self.manager.session(application_id, latest_url) as handle:
            for _ in range(self.policy.max_pages):
                adapter = await adapter_for(handle.page)
                snapshot = await adapter.inspect(handle.page)
                await self.manager.save_state(handle)
                self._checkpoint(lease, handle, snapshot, adapter.kind.value)
                if snapshot.challenges:
                    await self._wait(
                        lease,
                        memory,
                        {
                            "kind": "BROWSER_CHALLENGE",
                            "application_id": application_id,
                            "challenges": [value.value for value in snapshot.challenges],
                        },
                    )
                    return
                if (
                    not snapshot.supported_for_mutation
                    or adapter.kind.value not in self.policy.supported_mutation_ats
                ):
                    await self._wait(
                        lease,
                        memory,
                        {
                            "kind": "UNSUPPORTED_ATS",
                            "application_id": application_id,
                            "ats": adapter.kind.value,
                        },
                    )
                    return
                if snapshot.final_control_present and not snapshot.fields:
                    self._finish(lease, application_id, memory)
                    return
                planned, unresolved = await self._resolve(
                    lease, application_id, snapshot, artifacts, memory
                )
                if unresolved:
                    await self._wait(
                        lease,
                        memory,
                        {
                            "kind": "FORM_FIELD",
                            "application_id": application_id,
                            "page_key": snapshot.page_key,
                            "field_key": unresolved.field.field_key,
                            "question_text": unresolved.field.label,
                            "canonical_key": unresolved.canonical_key,
                            "local_only": unresolved.status == "LOCAL_ONLY",
                        },
                        question=unresolved.status != "LOCAL_ONLY",
                    )
                    return
                plan = self._plan(lease, application_id, snapshot, adapter.kind.value, planned)
                approval, created = self._approval(lease, application_id, memory, plan)
                self.queue.save_memory(lease, memory)
                if created or approval.approval_status == "PENDING":
                    await self._wait(
                        lease,
                        memory,
                        {
                            "kind": "PREPARE_APPROVAL",
                            "application_id": application_id,
                            "approval_id": approval.approval_id,
                            "plan_hash": approval.payload_sha256,
                        },
                        approval_id=approval.approval_id,
                    )
                    return
                if approval.approval_status not in {"APPROVED", "CONSUMED"}:
                    await self._wait(
                        lease,
                        memory,
                        {
                            "kind": "APPROVAL_BLOCKED",
                            "application_id": application_id,
                            "approval_id": approval.approval_id,
                            "status": approval.approval_status,
                        },
                    )
                    return
                self._revalidate_fields(lease, application_id, snapshot, planned)
                await self._apply(
                    lease,
                    application_id,
                    handle,
                    adapter,
                    snapshot,
                    planned,
                    approval,
                    approval.payload_sha256,
                )
                if snapshot.final_control_present:
                    self._finish(lease, application_id, memory)
                    return
                advanced = await adapter.advance(handle.page)
                await self.manager.save_state(handle)
                next_snapshot = await adapter.inspect(handle.page)
                self._checkpoint(lease, handle, next_snapshot, adapter.kind.value)
                if not advanced and not next_snapshot.final_control_present:
                    await self._wait(
                        lease,
                        memory,
                        {"kind": "NO_SAFE_ADVANCE", "application_id": application_id},
                    )
                    return
            raise FormPreparationError("form_page_limit_exceeded")
