"""Lease-fenced, approval-bound browser mutations with durable idempotency."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import select

from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import Approval, ExternalAction
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


class BrowserActionError(RuntimeError):
    """A browser action cannot be executed or safely reconciled."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def value_hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class ExternalActionService:
    """The only Phase 8 boundary that may invoke a modifying adapter method."""

    def __init__(self, queue: QueueService) -> None:
        self.queue = queue

    def _approval(
        self, approval: Approval, lease: Lease, application_id: str, plan_hash: str
    ) -> None:
        validate_id(approval.approval_id, IdKind.APPROVAL)
        if (
            approval.approval_type != "PREPARE_APPLICATION"
            or approval.application_id != application_id
            or approval.task_id != lease.task_id
            or approval.payload_sha256 != plan_hash
            or approval.approval_status not in {"APPROVED", "CONSUMED"}
        ):
            raise BrowserActionError("prepare_approval_invalid")
        if approval.valid_until:
            deadline = datetime.fromisoformat(approval.valid_until.replace("Z", "+00:00"))
            if self.queue.clock.now() >= deadline:
                if approval.approval_status == "APPROVED":
                    approval.approval_status = "EXPIRED"
                raise BrowserActionError("prepare_approval_expired")
        path = PathGuard().validate_write(PROJECT_ROOT / approval.payload_path)
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise BrowserActionError("prepare_approval_payload_unavailable") from None
        if actual != plan_hash:
            if approval.approval_status == "APPROVED":
                approval.approval_status = "REVOKED"
            raise BrowserActionError("prepare_approval_payload_changed")

    async def execute(
        self,
        lease: Lease,
        *,
        application_id: str,
        approval_id: str,
        plan_hash: str,
        action_type: str,
        idempotency_key: str,
        request: object,
        mutate: Callable[[], Awaitable[None]],
        confirm: Callable[[], Awaitable[bool]],
    ) -> str:
        request_hash = value_hash(request)
        external_id: str
        existing_status: str
        with self.queue.fence(lease) as session:
            approval = session.get(Approval, validate_id(approval_id, IdKind.APPROVAL))
            if approval is None:
                raise BrowserActionError("prepare_approval_missing")
            self._approval(approval, lease, application_id, plan_hash)
            existing = session.scalar(
                select(ExternalAction).where(ExternalAction.idempotency_key == idempotency_key)
            )
            now = format_utc(self.queue.clock.now())
            if existing:
                if (
                    existing.request_sha256 != request_hash
                    or existing.approval_id != approval_id
                    or existing.task_id != lease.task_id
                    or existing.application_id != application_id
                    or existing.action_type != action_type
                ):
                    raise BrowserActionError("browser_action_replay_conflict")
                external_id = existing.external_action_id
                existing_status = existing.action_status
            else:
                external_id = self.queue.ids.generate_ulid()
                existing_status = "PLANNED"
                session.add(
                    ExternalAction(
                        external_action_id=external_id,
                        application_id=application_id,
                        task_id=lease.task_id,
                        approval_id=approval_id,
                        action_type=action_type,
                        idempotency_key=idempotency_key,
                        request_sha256=request_hash,
                        action_status="PLANNED",
                        result_json=json.dumps({"plan_hash": plan_hash}),
                        created_at=now,
                        updated_at=now,
                    )
                )
                ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                    ActivityEvent(
                        "browser_action_planned",
                        task_id=lease.task_id,
                        application_id=application_id,
                        metadata={"external_action_id": external_id, "action_type": action_type},
                    )
                )
        if existing_status == "SUCCEEDED":
            if await confirm():
                return external_id
            # Browser reloads do not retain form controls or file inputs. Reapply only the
            # exact previously confirmed, reversible operation under its consumed approval.
            with self.queue.fence(lease) as session:
                approval = session.get(Approval, approval_id)
                row = session.get(ExternalAction, external_id)
                assert approval and row
                self._approval(approval, lease, application_id, plan_hash)
                row.action_status = "EXECUTING"
                row.updated_at = format_utc(self.queue.clock.now())
            try:
                await mutate()
                restored = await confirm()
            except Exception:
                restored = False
            with self.queue.fence(lease) as session:
                row = session.get(ExternalAction, external_id)
                assert row
                row.action_status = "SUCCEEDED" if restored else "UNKNOWN_RESULT"
                row.result_json = json.dumps(
                    {"plan_hash": plan_hash, "confirmed": restored, "reconciled": True}
                )
                row.updated_at = format_utc(self.queue.clock.now())
            if not restored:
                raise BrowserActionError("confirmed_browser_value_changed")
            return external_id
        if existing_status in {"EXECUTING", "UNKNOWN_RESULT"}:
            if not await confirm():
                with self.queue.fence(lease) as session:
                    row = session.get(ExternalAction, external_id)
                    assert row
                    row.action_status = "UNKNOWN_RESULT"
                    row.updated_at = format_utc(self.queue.clock.now())
                raise BrowserActionError("browser_action_result_unknown")
            with self.queue.fence(lease) as session:
                row = session.get(ExternalAction, external_id)
                assert row
                row.action_status = "SUCCEEDED"
                row.updated_at = format_utc(self.queue.clock.now())
            return external_id
        with self.queue.fence(lease) as session:
            approval = session.get(Approval, approval_id)
            assert approval
            self._approval(approval, lease, application_id, plan_hash)
            if approval.approval_status == "APPROVED":
                approval.approval_status = "CONSUMED"
                approval.consumed_at = format_utc(self.queue.clock.now())
            row = session.get(ExternalAction, external_id)
            assert row and row.action_status == "PLANNED"
            row.action_status = "EXECUTING"
            row.updated_at = format_utc(self.queue.clock.now())
        try:
            await mutate()
            confirmed = await confirm()
        except Exception:
            with self.queue.fence(lease) as session:
                row = session.get(ExternalAction, external_id)
                assert row
                row.action_status = "UNKNOWN_RESULT"
                row.updated_at = format_utc(self.queue.clock.now())
            raise
        with self.queue.fence(lease) as session:
            row = session.get(ExternalAction, external_id)
            assert row
            row.action_status = "SUCCEEDED" if confirmed else "UNKNOWN_RESULT"
            row.result_json = json.dumps({"plan_hash": plan_hash, "confirmed": confirmed})
            row.updated_at = format_utc(self.queue.clock.now())
            ActivityLogRepository(session, self.queue.clock, self.queue.ids).append(
                ActivityEvent(
                    "browser_action_finished",
                    task_id=lease.task_id,
                    application_id=application_id,
                    new_state=row.action_status,
                    metadata={"external_action_id": external_id, "action_type": action_type},
                )
            )
        if not confirmed:
            raise BrowserActionError("browser_action_not_confirmed")
        return external_id

    def recover(self) -> int:
        """Convert abandoned executing actions to an explicit unknown outcome."""
        with self.queue.database.transaction(immediate=True) as session:
            rows = list(
                session.scalars(
                    select(ExternalAction).where(
                        ExternalAction.action_type.in_(["BROWSER_FILL", "BROWSER_UPLOAD"]),
                        ExternalAction.action_status == "EXECUTING",
                    )
                )
            )
            now = format_utc(self.queue.clock.now())
            for row in rows:
                row.action_status = "UNKNOWN_RESULT"
                row.updated_at = now
            return len(rows)
