"""Lease-fenced ExternalActionService for native document mutations only.

Copy intent is durable before dispatch. An uncertain copy is reconciled by Drive
appProperties, never blindly repeated. Revision guards fence repeated edit requests.
"""

import json
from collections.abc import Callable

from sqlalchemy import select

from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import ExternalAction
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.models.prompts import digest
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.resume.documents import DocumentAdapter
from job_hunting_machine.resume.native import Json, paragraphs, validate_edit


class ReviewRequired(ValueError):
    """Human review is needed; do not advance the application or repeat uncertain effects."""


class ExternalActionService:
    def __init__(self, queue: QueueService, adapter: DocumentAdapter) -> None:
        self.queue, self.adapter = queue, adapter

    def _run(
        self,
        lease: Lease,
        key: str,
        request: Json,
        execute: Callable[[str, bool], str],
    ) -> str:
        identity = f"document:{lease.task_id}:{key}"
        with self.queue.fence(lease) as session:
            row = session.scalar(
                select(ExternalAction).where(ExternalAction.idempotency_key == identity)
            )
            fresh = row is None
            if row is None:
                now = format_utc(self.queue.clock.now())
                row = ExternalAction(
                    external_action_id=self.queue.ids.generate_ulid(),
                    task_id=lease.task_id,
                    application_id=self.queue.get(lease.task_id).application_id,
                    action_type="GOOGLE_DOCUMENT",
                    idempotency_key=identity,
                    request_sha256=digest(request),
                    action_status="EXECUTING",
                    result_json=json.dumps({"request": request}),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                ActivityLogRepository(session, self.queue.clock).append(
                    ActivityEvent(
                        "document_action_started",
                        task_id=lease.task_id,
                        application_id=row.application_id,
                        metadata={"action_id": row.external_action_id},
                    )
                )
            if row.request_sha256 != digest(request):
                raise ReviewRequired("document_action_replay_conflict")
            if row.action_status == "SUCCEEDED":
                assert row.external_reference
                return row.external_reference
            action_id = row.external_action_id
        # The request is sealed before the network boundary. Never hold a DB lock over HTTP.
        try:
            result = execute(action_id, fresh)
        except Exception:
            with self.queue.fence(lease) as session:
                row = session.get(ExternalAction, action_id)
                assert row
                row.action_status = "UNKNOWN_RESULT"
                row.updated_at = format_utc(self.queue.clock.now())
                ActivityLogRepository(session, self.queue.clock).append(
                    ActivityEvent(
                        "document_action_unknown",
                        task_id=lease.task_id,
                        application_id=row.application_id,
                        metadata={"action_id": action_id},
                    )
                )
            raise
        with self.queue.fence(lease) as session:
            row = session.get(ExternalAction, action_id)
            assert row
            row.action_status, row.external_reference = "SUCCEEDED", result
            row.updated_at = format_utc(self.queue.clock.now())
            ActivityLogRepository(session, self.queue.clock).append(
                ActivityEvent(
                    "document_action_completed",
                    task_id=lease.task_id,
                    application_id=row.application_id,
                    metadata={"action_id": action_id},
                )
            )
        return result

    def copy(self, lease: Lease, source_id: str, title: str) -> str:
        def dispatch(key: str, fresh: bool) -> str:
            found = self.adapter.find(key)
            if found:
                return found
            if not fresh:
                raise ReviewRequired("copy_outcome_unknown_manual_reconciliation_required")
            return (
                self.adapter.copy(source_id, title, key)
                if source_id
                else self.adapter.create(title, key)
            )

        return self._run(lease, "copy", {"source_id": source_id, "title": title}, dispatch)

    def edit(self, lease: Lease, before: Json, text: dict[str, str], round_number: int) -> None:
        def dispatch(key: str, fresh: bool) -> str:
            current = self.adapter.get(before["documentId"])
            try:
                validate_edit(before, current, text)
            except ValueError:
                if current["revisionId"] != before["revisionId"]:
                    raise ReviewRequired("document_changed_during_generation") from None
                self.adapter.edit(before, text)
                validate_edit(before, self.adapter.get(before["documentId"]), text)
            return str(before["documentId"])

        self._run(lease, f"edit-{round_number}", {"before": digest(before), "text": text}, dispatch)

    def fill(self, lease: Lease, before: Json, text: str, round_number: int = 0) -> None:
        def dispatch(key: str, fresh: bool) -> str:
            current = self.adapter.get(before["documentId"])
            actual = "".join(p.text for p in paragraphs(current))
            if actual != text + "\n":
                if current["revisionId"] != before["revisionId"]:
                    raise ReviewRequired("cover_letter_changed_during_generation")
                self.adapter.fill(before, text)
                if (
                    "".join(p.text for p in paragraphs(self.adapter.get(before["documentId"])))
                    != text + "\n"
                ):
                    raise ReviewRequired("cover_letter_content_mismatch")
            return str(before["documentId"])

        self._run(lease, f"fill-{round_number}", {"before": digest(before), "text": text}, dispatch)
