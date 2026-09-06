"""Durable inbox and decision processing. No callback executes an external action."""

import json
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_hunting_machine.clock import Clock, SystemClock, format_utc
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import Approval, ExternalAction, SlackEvent, TaskMemory
from job_hunting_machine.database.repositories import TaskCreate, TaskRepository
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import ConcurrentUpdateError
from job_hunting_machine.models.prompts import digest
from job_hunting_machine.orchestration.queue import LeaseLostError, QueueService
from job_hunting_machine.slack.actions import ExternalActionService
from job_hunting_machine.slack.approvals import ApprovalService
from job_hunting_machine.slack.config import SlackInputError, SlackSettings
from job_hunting_machine.slack.inbound import normalize
from job_hunting_machine.slack.messages import Notice, OutboundMessage, Question


class SlackControlPlane:
    def __init__(
        self,
        database: Database,
        settings: SlackSettings,
        *,
        actions: ExternalActionService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.database, self.settings = database, settings
        self.clock = clock or SystemClock()
        self.queue = QueueService(database, clock=self.clock)
        self.ids = self.queue.ids
        self.actions = actions or ExternalActionService(database, settings, clock=self.clock)
        self.approvals = ApprovalService(settings, self.clock)

    def receive(self, body: dict[str, Any]) -> str:
        """Persist sanitized work BEFORE Bolt acknowledges. No model/file/network processing."""
        value = normalize(body, self.settings)
        event_id = str(value.pop("provider_event_id", ""))
        payload_hash = digest(value)
        event_id = event_id or f"interaction_{payload_hash}"
        event_type = "message" if value["kind"] == "urls" else f"interaction:{payload_hash}"
        with self.database.transaction(immediate=True) as session:
            existing = session.get(SlackEvent, event_id) or session.scalar(
                select(SlackEvent).where(
                    SlackEvent.channel_id == value["channel"],
                    SlackEvent.message_ts == value["message_ts"],
                    SlackEvent.event_type == event_type,
                )
            )
            if existing:
                if existing.payload_sha256 != payload_hash:
                    raise SlackInputError("slack_event_identity_conflict")
                return existing.slack_event_id
            session.add(
                SlackEvent(
                    slack_event_id=event_id,
                    event_type=event_type,
                    channel_id=value["channel"],
                    message_ts=value["message_ts"],
                    thread_ts=value.get("thread_ts"),
                    user_id=value["user"],
                    payload_sha256=payload_hash,
                    processing_status="PENDING",
                    received_at=format_utc(self.clock.now()),
                )
            )
            session.flush()
            TaskRepository(session, self.clock, self.ids).create(
                TaskCreate(
                    "SLACK_PROCESS_EVENT",
                    dedupe_key=f"slack_event:{event_id}",
                    task_status="READY",
                    payload={"slack_event_id": event_id, "event": value},
                )
            )
            return event_id

    def process_one(self) -> bool:
        lease = self.queue.claim(["SLACK_PROCESS_EVENT"])
        if lease is None:
            return False
        try:
            with self.queue.fence(lease) as session:
                event = session.get(SlackEvent, lease.payload["slack_event_id"])
                if event is None:
                    raise SlackInputError("missing_durable_event")
                if event.processing_status == "PENDING":
                    value = lease.payload["event"]
                    if not isinstance(value, dict) or digest(value) != event.payload_sha256:
                        raise SlackInputError("stored_event_hash_mismatch")
                    try:
                        # Recheck the current allowlist after a restart/configuration change.
                        self.settings.authorize(
                            team=value["team"],
                            app=value["app"],
                            channel=value["channel"],
                            user=value["user"],
                        )
                        with session.begin_nested():
                            self._process(session, value, event.slack_event_id)
                        event.processing_status = "PROCESSED"
                    except (SlackInputError, ConcurrentUpdateError):
                        event.processing_status = "REJECTED"
                    event.processed_at = format_utc(self.clock.now())
                    ActivityLogRepository(session, self.clock, self.ids).append(
                        ActivityEvent(
                            "slack_event_processed",
                            task_id=lease.task_id,
                            new_state=event.processing_status,
                            metadata={"slack_event_id": event.slack_event_id},
                        )
                    )
            # A crash here is safe: the next attempt sees the completed inbox record.
            self.queue.complete(lease, {"slack_event_id": lease.payload["slack_event_id"]})
        except LeaseLostError:
            pass
        except Exception:
            self.queue.fail(lease, retryable=True)
        return True

    def _process(self, session: Session, value: dict[str, Any], event_id: str) -> None:
        if value["kind"] == "urls":
            if value["urls"]:
                TaskRepository(session, self.clock, self.ids).create(
                    TaskCreate(
                        "RETRIEVE_LINKS",
                        dedupe_key=f"retrieve_links:{event_id}",
                        task_status="READY",
                        payload={"slack_event_id": event_id, "urls": value["urls"]},
                    )
                )
            return
        action = session.get(ExternalAction, value["external_action_id"])
        if (
            not action
            or action.action_type != "SLACK_NOTIFICATION"
            or action.action_status != "SUCCEEDED"
        ):
            raise SlackInputError("interaction_message_not_confirmed")
        data = json.loads(action.result_json or "{}")
        message = OutboundMessage.model_validate(data["request"])
        if digest(message.model_dump(mode="json")) != action.request_sha256:
            raise SlackInputError("interaction_payload_changed")
        receipt = data.get("receipt", {})
        if (
            value["channel"] != message.channel
            or value["channel"] != receipt.get("channel")
            or value["message_ts"] != receipt.get("message_ts")
        ):
            raise SlackInputError("interaction_message_mismatch")
        if value["kind"] == "jhm_answer":
            if message.kind != "question" or not message.interrupt_id:
                raise SlackInputError("interaction_kind_mismatch")
            self.queue.resume_in_transaction(
                session,
                message.task_id,
                message.interrupt_id,
                {"answer": value["answer"], "user_id": value["user"], "event_id": event_id},
            )
            return
        if message.kind != "approval":
            raise SlackInputError("interaction_kind_mismatch")
        approval = session.get(Approval, message.approval_id)
        if (
            not approval
            or approval.task_id != message.task_id
            or approval.application_id != message.application_id
            or approval.payload_sha256 != message.payload_sha256
            or approval.slack_channel_id != message.channel
            or approval.slack_message_ts != value["message_ts"]
        ):
            raise SlackInputError("approval_correlation_mismatch")
        self.approvals.decide(
            session,
            approval,
            decision="APPROVED" if value["kind"] == "jhm_approve" else "REJECTED",
            user_id=value["user"],
        )
        memory_row = session.get(TaskMemory, message.task_id)
        memory = json.loads(memory_row.state_json) if memory_row else {}
        interrupts = memory.get("interrupts", {})
        if isinstance(interrupts, dict):
            match = next(
                (
                    interrupt_id
                    for interrupt_id, item in interrupts.items()
                    if isinstance(item, dict) and item.get("approval_id") == approval.approval_id
                ),
                None,
            )
            if match:
                self.queue.resume_in_transaction(
                    session,
                    message.task_id,
                    match,
                    {
                        "approval_id": approval.approval_id,
                        "decision": approval.approval_status,
                        "user_id": value["user"],
                        "event_id": event_id,
                    },
                )

    def notify(self, task_id: str, notice: Notice) -> str:
        task = self.queue.get(task_id)
        return self.actions.plan(
            OutboundMessage(
                kind="notice",
                channel=self._channel(),
                task_id=task_id,
                application_id=task.application_id,
                notice=notice,
            ),
            f"notice:{task_id}:{task.version}:{notice.value}",
        )

    def request_approval(self, approval_id: str) -> str:
        with self.database.transaction() as session:
            approval = session.get(Approval, approval_id)
            if not approval or approval.approval_status != "PENDING":
                raise SlackInputError("approval_not_pending")
            self.approvals.verify(session, approval)
            message = OutboundMessage(
                kind="approval",
                channel=self._channel(),
                task_id=approval.task_id or "",
                application_id=approval.application_id,
                approval_id=approval_id,
                approval_type=cast(
                    Literal[
                        "PREPARE_APPLICATION",
                        "SUBMIT_APPLICATION",
                        "SEND_EMAIL",
                        "SEND_EXTERNAL_MESSAGE",
                    ],
                    approval.approval_type,
                ),
                payload_sha256=approval.payload_sha256,
            )
        return self.actions.plan(message, f"approval:{approval_id}:{approval.payload_sha256}")

    def ask_missing(self, task_id: str, interrupt_id: str, question: Question) -> str:
        task = self.queue.get(task_id)
        interrupts = self.queue.memory(task_id).get("interrupts", {})
        if (
            task.task_status != "WAITING_HUMAN"
            or not isinstance(interrupts, dict)
            or interrupt_id not in interrupts
        ):
            raise SlackInputError("human_interrupt_not_pending")
        return self.actions.plan(
            OutboundMessage(
                kind="question",
                channel=self._channel(),
                task_id=task_id,
                application_id=task.application_id,
                interrupt_id=interrupt_id,
                question=question,
            ),
            f"question:{task_id}:{interrupt_id}",
        )

    def _channel(self) -> str:
        if not self.settings.notification_channel_id:
            raise SlackInputError("notification_channel_not_configured")
        return self.settings.notification_channel_id

    def recover(self) -> int:
        return self.queue.recover() + self.actions.recover()
