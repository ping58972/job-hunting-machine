"""Persist pending approval metadata, without deciding or executing approval."""

from dataclasses import dataclass

from job_hunting_machine.database.models import Approval
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.artifacts import local_record_path, validate_sha256
from job_hunting_machine.database.repositories.base import ReplayConflictError, Repository
from job_hunting_machine.ids import IdKind, validate_id

_APPROVAL_TYPES = frozenset(
    {"PREPARE_APPLICATION", "SUBMIT_APPLICATION", "SEND_EMAIL", "SEND_EXTERNAL_MESSAGE"}
)


@dataclass(frozen=True, slots=True)
class ApprovalCreate:
    approval_type: str
    payload_path: str
    payload_sha256: str
    application_id: str | None = None
    task_id: str | None = None
    valid_until: str | None = None
    approval_id: str | None = None


class ApprovalRepository(Repository):
    def get(self, approval_id: str) -> Approval | None:
        return self.session.get(Approval, validate_id(approval_id, IdKind.APPROVAL))

    def create(self, data: ApprovalCreate) -> Approval:
        if data.approval_type not in _APPROVAL_TYPES:
            raise ValueError("Invalid approval type")
        path = local_record_path(data.payload_path)
        validate_sha256(data.payload_sha256)
        for value, kind in (
            (data.approval_id, IdKind.APPROVAL),
            (data.application_id, IdKind.APPLICATION),
            (data.task_id, IdKind.TASK),
        ):
            if value is not None:
                validate_id(value, kind)
        existing = self.get(data.approval_id) if data.approval_id is not None else None
        if existing is not None:
            if (
                existing.approval_type != data.approval_type
                or existing.payload_path != path
                or existing.payload_sha256 != data.payload_sha256
                or existing.application_id != data.application_id
                or existing.task_id != data.task_id
                or existing.valid_until != data.valid_until
            ):
                raise ReplayConflictError("Approval ID was reused for a different payload")
            return existing
        with self.session.begin_nested():
            record = Approval(
                approval_id=data.approval_id or self.ids.new(IdKind.APPROVAL),
                application_id=data.application_id,
                task_id=data.task_id,
                approval_type=data.approval_type,
                approval_status="PENDING",
                payload_path=path,
                payload_sha256=data.payload_sha256,
                requested_via="SLACK",
                requested_at=self.timestamp(),
                valid_until=data.valid_until,
            )
            self.session.add(record)
            self.session.flush()
            ActivityLogRepository(self.session, self.clock, self.ids).append(
                ActivityEvent(
                    "approval_created",
                    application_id=data.application_id,
                    task_id=data.task_id,
                    new_state="PENDING",
                    metadata={"approval_id": record.approval_id},
                )
            )
        return record
