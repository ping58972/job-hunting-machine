"""The Phase 4 ExternalActionService supports Slack notifications ONLY."""

import json
from datetime import timedelta

from sqlalchemy import select

from job_hunting_machine.clock import Clock, SystemClock, format_utc
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import AgentTask, Approval, ExternalAction
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.ids import IdGenerator
from job_hunting_machine.models.prompts import digest
from job_hunting_machine.slack.adapter import DeliveryError, FakeSlackAdapter, SlackAdapter
from job_hunting_machine.slack.config import SlackInputError, SlackSettings
from job_hunting_machine.slack.messages import OutboundMessage


class ExternalActionService:
    def __init__(
        self,
        database: Database,
        settings: SlackSettings,
        *,
        adapter: SlackAdapter | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.database, self.settings = database, settings
        self.adapter = adapter or FakeSlackAdapter()
        self.clock = clock or SystemClock()
        self.ids = IdGenerator(self.clock)

    def plan(self, message: OutboundMessage, dedupe_key: str) -> str:
        if message.channel not in self.settings.allowed_channel_ids:
            raise SlackInputError("outbound_channel_not_allowed")
        request = message.model_dump(mode="json")
        request_hash = digest(request)
        key = f"slack:{digest(dedupe_key)}"
        with self.database.transaction(immediate=True) as session:
            existing = session.scalar(
                select(ExternalAction).where(ExternalAction.idempotency_key == key)
            )
            if existing:
                if existing.request_sha256 != request_hash:
                    raise SlackInputError("notification_dedupe_conflict")
                return existing.external_action_id
            task = session.get(AgentTask, message.task_id)
            if not task or task.application_id != message.application_id:
                raise SlackInputError("notification_task_mismatch")
            if message.approval_id:
                approval = session.get(Approval, message.approval_id)
                if (
                    not approval
                    or approval.task_id != message.task_id
                    or approval.application_id != message.application_id
                    or approval.payload_sha256 != message.payload_sha256
                ):
                    raise SlackInputError("notification_approval_mismatch")
            identifier = self.ids.generate_ulid()
            now = format_utc(self.clock.now())
            session.add(
                ExternalAction(
                    external_action_id=identifier,
                    task_id=message.task_id,
                    application_id=message.application_id,
                    approval_id=message.approval_id,
                    action_type="SLACK_NOTIFICATION",
                    idempotency_key=key,
                    request_sha256=request_hash,
                    action_status="PLANNED",
                    result_json=json.dumps({"request": request}),
                    created_at=now,
                    updated_at=now,
                )
            )
            ActivityLogRepository(session, self.clock, self.ids).append(
                ActivityEvent(
                    "slack_notification_planned",
                    task_id=message.task_id,
                    application_id=message.application_id,
                    metadata={"external_action_id": identifier},
                )
            )
            return identifier

    def deliver_one(self) -> bool:
        with self.database.transaction(immediate=True) as session:
            row = session.scalar(
                select(ExternalAction)
                .where(
                    ExternalAction.action_type == "SLACK_NOTIFICATION",
                    ExternalAction.action_status == "PLANNED",
                )
                .order_by(ExternalAction.created_at, ExternalAction.external_action_id)
                .limit(1)
            )
            if row is None:
                return False
            data = json.loads(row.result_json or "{}")
            message = OutboundMessage.model_validate(data["request"])
            if digest(message.model_dump(mode="json")) != row.request_sha256:
                raise SlackInputError("notification_hash_mismatch")
            if message.channel not in self.settings.allowed_channel_ids:
                raise SlackInputError("outbound_channel_not_allowed")
            identifier = row.external_action_id
            owner = self.ids.generate_ulid()
            data.update(
                owner=owner, expires_at=format_utc(self.clock.now() + timedelta(seconds=60))
            )
            row.result_json = json.dumps(data)
            row.action_status = "EXECUTING"
            row.updated_at = format_utc(self.clock.now())
            ActivityLogRepository(session, self.clock, self.ids).append(
                ActivityEvent(
                    "slack_notification_started",
                    task_id=row.task_id,
                    application_id=row.application_id,
                    metadata={"external_action_id": identifier},
                )
            )
        status = "SUCCEEDED"
        receipt = None
        try:
            receipt = self.adapter.post(message.render(identifier), identifier)
        except DeliveryError as error:
            status = "UNKNOWN_RESULT" if error.unknown else "FAILED"
        except Exception:
            status = "UNKNOWN_RESULT"
        with self.database.transaction(immediate=True) as session:
            row = session.get(ExternalAction, identifier)
            assert row
            data = json.loads(row.result_json or "{}")
            if row.action_status != "EXECUTING" or data.get("owner") != owner:
                return True
            if receipt:
                if receipt.channel != message.channel:
                    status = "UNKNOWN_RESULT"
                else:
                    data["receipt"] = {"channel": receipt.channel, "message_ts": receipt.message_ts}
                    row.external_reference = receipt.message_ts
                    if row.approval_id:
                        approval = session.get(Approval, row.approval_id)
                        assert approval
                        approval.slack_channel_id = receipt.channel
                        approval.slack_message_ts = receipt.message_ts
            row.action_status = status
            row.result_json = json.dumps(data)
            row.updated_at = format_utc(self.clock.now())
            ActivityLogRepository(session, self.clock, self.ids).append(
                ActivityEvent(
                    "slack_notification_finished",
                    task_id=row.task_id,
                    application_id=row.application_id,
                    new_state=status,
                    metadata={"external_action_id": identifier},
                )
            )
        return True

    def recover(self) -> int:
        with self.database.transaction(immediate=True) as session:
            rows = list(
                session.scalars(
                    select(ExternalAction).where(
                        ExternalAction.action_type == "SLACK_NOTIFICATION",
                        ExternalAction.action_status == "EXECUTING",
                    )
                )
            )
            count = 0
            for row in rows:
                data = json.loads(row.result_json or "{}")
                if data.get("expires_at", "") > format_utc(self.clock.now()):
                    continue
                row.action_status = "UNKNOWN_RESULT"
                row.updated_at = format_utc(self.clock.now())
                ActivityLogRepository(session, self.clock, self.ids).append(
                    ActivityEvent(
                        "slack_delivery_unknown",
                        task_id=row.task_id,
                        application_id=row.application_id,
                        metadata={"external_action_id": row.external_action_id},
                    )
                )
                count += 1
            return count
