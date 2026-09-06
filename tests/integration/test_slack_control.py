"""Phase 4 acceptance with real SQLite/Bolt and a network-free Slack adapter."""

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from slack_bolt.request import BoltRequest
from sqlalchemy import func, select

from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    ActivityLog,
    AgentTask,
    ApplicationPipeline,
    Approval,
    ExternalAction,
    Job,
    SlackEvent,
)
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationRepository,
    ApprovalCreate,
    ApprovalRepository,
    JobCreate,
    JobRepository,
    TaskCreate,
    TaskRepository,
)
from job_hunting_machine.security.paths import PathGuard
from job_hunting_machine.slack import (
    ExternalActionService,
    FakeSlackAdapter,
    Notice,
    Question,
    SlackControlPlane,
    SlackInputError,
    SlackSettings,
)
from job_hunting_machine.slack.adapter import DeliveryError
from job_hunting_machine.slack.socket_mode import build_bolt_app, run_live

CLOCK = FrozenClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
SETTINGS = SlackSettings(
    team_id="TTEST",
    app_id="ATEST",
    authorized_user_ids=frozenset({"UALLOWED"}),
    allowed_channel_ids=frozenset({"CTEST"}),
    notification_channel_id="CTEST",
)


@pytest.fixture
def control(tmp_path: Path) -> Iterator[SlackControlPlane]:
    database = Database(tmp_path / "slack.db")
    database.migrate()
    actions = ExternalActionService(database, SETTINGS, adapter=FakeSlackAdapter(), clock=CLOCK)
    yield SlackControlPlane(database, SETTINGS, actions=actions, clock=CLOCK)
    database.dispose()


def envelope(**changes: Any) -> dict[str, Any]:
    body = {
        "type": "event_callback",
        "team_id": "TTEST",
        "api_app_id": "ATEST",
        "event_id": "EvFixture",
        "event": {
            "type": "message",
            "user": "UALLOWED",
            "channel": "CTEST",
            "ts": "1700000000.123456",
            "text": "<https://example.invalid/jobs/1|job>",
        },
    }
    body.update(changes)
    return body


def approval_fixture(control: SlackControlPlane, path: Path) -> tuple[str, str, str]:
    PathGuard().write_text(path, '{"synthetic_review":true}')
    with control.database.transaction() as session:
        job = JobRepository(session, CLOCK).create(
            JobCreate(
                original_url="https://example.invalid/job",
                canonical_url="https://example.invalid/job",
                source_type="TEST",
                company_name="Synthetic",
                job_title="Synthetic",
                qualification_status="PASSED",
            )
        )
        application = ApplicationRepository(session, CLOCK).create_from_passed_job(
            job.job_id, ApplicationCreate("Synthetic", "Synthetic", job.canonical_url)
        )
        pipeline = session.get(ApplicationPipeline, application.application_id)
        assert pipeline
        pipeline.application_status = (
            "READY_TO_REVIEW"  # Synthetic fixture, no real workflow transition.
        )
        task = TaskRepository(session, CLOCK).create(
            TaskCreate("FORM_PROCESS", application_id=application.application_id)
        )
        approval = ApprovalRepository(session, CLOCK).create(
            ApprovalCreate(
                "SUBMIT_APPLICATION",
                str(path),
                hashlib.sha256(path.read_bytes()).hexdigest(),
                application_id=application.application_id,
                task_id=task.task_id,
            )
        )
        return approval.approval_id, task.task_id, application.application_id


def interaction(
    control: SlackControlPlane, action_id: str, decision: str = "approve", user: str = "UALLOWED"
) -> dict[str, Any]:
    with control.database.transaction() as session:
        action = session.get(ExternalAction, action_id)
        assert action
        data = json.loads(action.result_json or "{}")
        receipt = data["receipt"]
    return {
        "type": "block_actions",
        "team": {"id": "TTEST"},
        "api_app_id": "ATEST",
        "user": {"id": user},
        "channel": {"id": "CTEST"},
        "container": {
            "type": "message",
            "channel_id": "CTEST",
            "message_ts": receipt["message_ts"],
        },
        "actions": [
            {
                "type": "button",
                "action_id": f"jhm_{decision}",
                "action_ts": "1700000001.000001",
                "value": action_id,
            }
        ],
    }


def status(control: SlackControlPlane, approval_id: str) -> str:
    with control.database.transaction() as session:
        row = session.get(Approval, approval_id)
        assert row
        return row.approval_status


def test_duplicate_event_processed_once_without_creating_jobs(control: SlackControlPlane) -> None:
    first = control.receive(envelope())
    assert control.receive(envelope()) == first
    assert control.process_one()
    assert not control.process_one()
    with control.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SlackEvent)) == 1
        tasks = list(
            session.scalars(select(AgentTask).where(AgentTask.task_type == "RETRIEVE_LINKS"))
        )
        assert len(tasks) == 1
        assert json.loads(tasks[0].payload_json or "{}")["urls"] == [
            "https://example.invalid/jobs/1"
        ]
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_pending_event_survives_restart(control: SlackControlPlane) -> None:
    event_id = control.receive(envelope())
    reopened = Database(control.database.path)
    try:
        restarted = SlackControlPlane(reopened, SETTINGS, clock=CLOCK)
        restarted.recover()
        assert restarted.process_one()
        with reopened.transaction() as session:
            event = session.get(SlackEvent, event_id)
            assert event and event.processing_status == "PROCESSED"
    finally:
        reopened.dispose()


def test_approval_correlates_and_never_submits(control: SlackControlPlane, tmp_path: Path) -> None:
    approval_id, task_id, application_id = approval_fixture(control, tmp_path / "review.json")
    action_id = control.request_approval(approval_id)
    assert control.actions.deliver_one()
    body = interaction(control, action_id)
    control.receive(body)
    assert control.process_one()
    assert status(control, approval_id) == "APPROVED"
    with control.database.transaction() as session:
        row = session.get(Approval, approval_id)
        assert row and row.task_id == task_id and row.application_id == application_id
        assert row.decided_by_slack_user_id == "UALLOWED" and row.consumed_at is None
        pipeline = session.get(ApplicationPipeline, application_id)
        assert pipeline and pipeline.application_status == "READY_TO_REVIEW"
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExternalAction)
                .where(ExternalAction.action_type != "SLACK_NOTIFICATION")
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentTask)
                .where(AgentTask.task_type == "SUBMIT_APPLICATION")
            )
            == 0
        )
    control.receive(body)
    assert not control.process_one()
    with control.database.transaction() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ActivityLog)
                .where(ActivityLog.event_type == "slack_approval_decided")
            )
            == 1
        )


def test_unauthorized_user_cannot_approve(control: SlackControlPlane, tmp_path: Path) -> None:
    approval_id, _, _ = approval_fixture(control, tmp_path / "review.json")
    action = control.request_approval(approval_id)
    control.actions.deliver_one()
    with pytest.raises(SlackInputError, match="unauthorized"):
        control.receive(interaction(control, action, user="UOTHER"))
    assert status(control, approval_id) == "PENDING"


def test_pending_approval_and_callback_survive_restart(
    control: SlackControlPlane, tmp_path: Path
) -> None:
    approval_id, _, _ = approval_fixture(control, tmp_path / "review.json")
    action = control.request_approval(approval_id)
    control.actions.deliver_one()
    control.receive(interaction(control, action))
    restarted = SlackControlPlane(control.database, SETTINGS, clock=CLOCK)
    assert status(restarted, approval_id) == "PENDING"
    restarted.recover()
    restarted.process_one()
    assert status(restarted, approval_id) == "APPROVED"


def test_wrong_message_cannot_approve(control: SlackControlPlane, tmp_path: Path) -> None:
    approval_id, _, _ = approval_fixture(control, tmp_path / "review.json")
    action = control.request_approval(approval_id)
    control.actions.deliver_one()
    body = interaction(control, action)
    body["container"]["message_ts"] = "1700000000.999999"
    event_id = control.receive(body)
    control.process_one()
    assert status(control, approval_id) == "PENDING"
    with control.database.transaction() as session:
        event = session.get(SlackEvent, event_id)
        assert event and event.processing_status == "REJECTED"


def test_mutated_review_hash_rejects_approval(control: SlackControlPlane, tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    approval_id, _, _ = approval_fixture(control, path)
    action = control.request_approval(approval_id)
    control.actions.deliver_one()
    PathGuard().write_text(path, '{"changed":true}')
    control.receive(interaction(control, action))
    control.process_one()
    assert status(control, approval_id) == "PENDING"


def test_reject_records_decision_only(control: SlackControlPlane, tmp_path: Path) -> None:
    approval_id, _, _ = approval_fixture(control, tmp_path / "review.json")
    action = control.request_approval(approval_id)
    control.actions.deliver_one()
    control.receive(interaction(control, action, decision="reject"))
    control.process_one()
    assert status(control, approval_id) == "REJECTED"


@pytest.mark.parametrize(
    "body",
    [{}, {"type": "block_actions"}, {"type": "block_actions", "actions": []}, {"type": "unknown"}],
)
def test_malformed_interaction_rejected(control: SlackControlPlane, body: dict[str, Any]) -> None:
    with pytest.raises(SlackInputError):
        control.receive(body)
    with control.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SlackEvent)) == 0


def test_fake_adapter_deduplicates_notification(control: SlackControlPlane) -> None:
    task_id = control.queue.enqueue(TaskCreate("FAKE"))
    action = control.notify(task_id, Notice.SUCCEEDED)
    assert control.notify(task_id, Notice.SUCCEEDED) == action
    assert control.actions.deliver_one()
    assert not control.actions.deliver_one()
    assert isinstance(control.actions.adapter, FakeSlackAdapter)
    assert len(control.actions.adapter.messages) == 1


def test_missing_info_reply_durable_and_not_echoed(control: SlackControlPlane) -> None:
    task_id = control.queue.enqueue(TaskCreate("FAKE"))
    lease = control.queue.claim(["FAKE"])
    assert lease
    control.queue.complete(
        lease, {"interrupts": {"interrupt_fixture": {"question": "fixture"}}}, waiting=True
    )
    action = control.ask_missing(task_id, "interrupt_fixture", Question.AVAILABILITY)
    control.actions.deliver_one()
    body = interaction(control, action, decision="answer")
    body["state"] = {
        "values": {"jhm_answer_block": {"jhm_answer_input": {"value": "Synthetic availability"}}}
    }
    control.receive(body)
    control.process_one()
    assert control.queue.get(task_id).task_status == "READY"
    resume = control.queue.memory(task_id)["resume"]
    assert isinstance(resume, dict)
    value = resume["value"]
    assert isinstance(value, dict)
    assert resume["interrupt_id"] == "interrupt_fixture"
    assert value == {
        "answer": "Synthetic availability",
        "user_id": "UALLOWED",
        "event_id": value["event_id"],
    }
    assert str(value["event_id"]).startswith("interaction_")


def test_prepare_approval_callback_resumes_only_the_exact_task(
    control: SlackControlPlane, tmp_path: Path
) -> None:
    payload = PathGuard().write_text(tmp_path / "prepare.json", '{"operation":"prepare"}')
    with control.database.transaction() as session:
        job = JobRepository(session, CLOCK).create(
            JobCreate(
                "https://example.invalid/prepare",
                "https://example.invalid/prepare",
                "TEST",
                qualification_status="PASSED",
            )
        )
        application = ApplicationRepository(session, CLOCK).create_from_passed_job(
            job.job_id,
            ApplicationCreate("Synthetic", "Role", job.canonical_url),
        )
        task = TaskRepository(session, CLOCK).create(
            TaskCreate(
                "FORM_PROCESS",
                task_status="READY",
                application_id=application.application_id,
            )
        )
        approval = ApprovalRepository(session, CLOCK).create(
            ApprovalCreate(
                "PREPARE_APPLICATION",
                str(payload),
                hashlib.sha256(payload.read_bytes()).hexdigest(),
                application_id=application.application_id,
                task_id=task.task_id,
            )
        )
        task_id, approval_id = task.task_id, approval.approval_id
    lease = control.queue.claim(["FORM_PROCESS"])
    assert lease and lease.task_id == task_id
    control.queue.complete(
        lease,
        {
            "interrupts": {
                "prepare_fixture": {
                    "kind": "PREPARE_APPROVAL",
                    "approval_id": approval_id,
                }
            }
        },
        waiting=True,
    )
    action_id = control.request_approval(approval_id)
    control.actions.deliver_one()
    control.receive(interaction(control, action_id))
    control.process_one()
    assert status(control, approval_id) == "APPROVED"
    assert control.queue.get(task_id).task_status == "READY"
    memory = control.queue.memory(task_id)
    resume = memory["resume"]
    assert isinstance(resume, dict)
    assert resume["interrupt_id"] == "prepare_fixture"
    with control.database.transaction() as session:
        types = set(session.scalars(select(ExternalAction.action_type)))
        assert types == {"SLACK_NOTIFICATION"}
    assert isinstance(control.actions.adapter, FakeSlackAdapter)
    assert "Synthetic availability" not in json.dumps(control.actions.adapter.messages)


def test_secret_text_not_persisted_or_sent(control: SlackControlPlane) -> None:
    body = envelope()
    body["event"]["text"] = "password=never-store https://example.invalid/jobs/1?token=never-store"
    control.receive(body)
    control.process_one()
    with control.database.transaction() as session:
        payloads = list(session.scalars(select(AgentTask.payload_json)))
        assert "never-store" not in json.dumps(payloads)
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentTask)
                .where(AgentTask.task_type == "RETRIEVE_LINKS")
            )
            == 0
        )


def test_live_slack_requires_explicit_opt_in(
    control: SlackControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLACK_ALLOW_LIVE", raising=False)
    with pytest.raises(SlackInputError, match="SLACK_ALLOW_LIVE"):
        run_live(control.database, settings=SETTINGS)


def test_bolt_socket_dispatch_persists_before_ack(control: SlackControlPlane) -> None:
    app = build_bolt_app(control)
    response = app.dispatch(BoltRequest(body=envelope(), mode="socket_mode"))
    assert response.status == 200
    with control.database.transaction() as session:
        event = session.get(SlackEvent, "EvFixture")
        assert event and event.processing_status == "PENDING"


def test_unknown_delivery_is_not_automatically_resent(control: SlackControlPlane) -> None:
    class LostResponse(FakeSlackAdapter):
        def post(self, message: dict[str, Any], delivery_key: str) -> Any:
            super().post(message, delivery_key)
            raise DeliveryError()

    control.actions.adapter = LostResponse()
    task_id = control.queue.enqueue(TaskCreate("FAKE"))
    identifier = control.notify(task_id, Notice.FAILED)
    control.actions.deliver_one()
    assert not control.actions.deliver_one()
    with control.database.transaction() as session:
        action = session.get(ExternalAction, identifier)
        assert action and action.action_status == "UNKNOWN_RESULT"


def test_concurrent_duplicate_deliveries_create_one_inbox_task(control: SlackControlPlane) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def receive() -> str:
        barrier.wait()
        return control.receive(envelope())

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: receive(), range(2)))
    assert results[0] == results[1]
    with control.database.transaction() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentTask)
                .where(AgentTask.task_type == "SLACK_PROCESS_EVENT")
            )
            == 1
        )


def test_storage_failure_is_not_acknowledged(
    control: SlackControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(body: dict[str, Any]) -> str:
        raise RuntimeError("Synthetic persistence failure")

    monkeypatch.setattr(control, "receive", fail)
    app = build_bolt_app(control)
    result = app.dispatch(BoltRequest(body=envelope(), mode="socket_mode"))
    assert result.status == 500


def test_bolt_action_ack_follows_durable_receive(
    control: SlackControlPlane, tmp_path: Path
) -> None:
    approval_id, _, _ = approval_fixture(control, tmp_path / "review.json")
    action = control.request_approval(approval_id)
    control.actions.deliver_one()
    app = build_bolt_app(control)
    result = app.dispatch(BoltRequest(body=interaction(control, action), mode="socket_mode"))
    assert result.status == 200
    assert status(control, approval_id) == "PENDING"
    assert control.process_one()
    assert status(control, approval_id) == "APPROVED"


def test_expired_approval_cannot_be_used(control: SlackControlPlane, tmp_path: Path) -> None:
    approval_id, _, _ = approval_fixture(control, tmp_path / "review.json")
    action = control.request_approval(approval_id)
    control.actions.deliver_one()
    with control.database.transaction() as session:
        approval = session.get(Approval, approval_id)
        assert approval
        approval.valid_until = "2026-09-05T11:59:00.000Z"
    control.receive(interaction(control, action))
    control.process_one()
    assert status(control, approval_id) == "PENDING"


def test_allowlist_revocation_is_checked_after_restart(
    control: SlackControlPlane, tmp_path: Path
) -> None:
    approval_id, _, _ = approval_fixture(control, tmp_path / "review.json")
    action = control.request_approval(approval_id)
    control.actions.deliver_one()
    event_id = control.receive(interaction(control, action))
    revoked = SETTINGS.model_copy(update={"authorized_user_ids": frozenset()})
    restarted = SlackControlPlane(control.database, revoked, clock=CLOCK)
    restarted.process_one()
    assert status(control, approval_id) == "PENDING"
    with control.database.transaction() as session:
        event = session.get(SlackEvent, event_id)
        assert event and event.processing_status == "REJECTED"


def test_restart_after_commit_does_not_repeat_decision(
    control: SlackControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_hunting_machine.orchestration.queue import LeaseLostError

    approval_id, _, _ = approval_fixture(control, tmp_path / "review.json")
    action = control.request_approval(approval_id)
    control.actions.deliver_one()
    control.receive(interaction(control, action))

    def lost(*args: Any, **kwargs: Any) -> None:
        raise LeaseLostError("Simulated death after inbox transaction committed")

    monkeypatch.setattr(control.queue, "complete", lost)
    control.process_one()
    assert status(control, approval_id) == "APPROVED"
    later = FrozenClock(datetime(2026, 9, 5, 13, tzinfo=UTC))
    restarted = SlackControlPlane(control.database, SETTINGS, clock=later)
    assert restarted.recover() == 1
    assert restarted.process_one()
    with control.database.transaction() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ActivityLog)
                .where(ActivityLog.event_type == "slack_approval_decided")
            )
            == 1
        )


def test_pending_notification_survives_restart(control: SlackControlPlane) -> None:
    identifier = control.notify(control.queue.enqueue(TaskCreate("FAKE")), Notice.FAILED)
    restarted = ExternalActionService(control.database, SETTINGS, clock=CLOCK)
    assert restarted.deliver_one()
    with control.database.transaction() as session:
        action = session.get(ExternalAction, identifier)
        assert action and action.action_status == "SUCCEEDED"


def test_interrupted_delivery_recovers_as_unknown(control: SlackControlPlane) -> None:
    class SimulatedDeath(BaseException):
        pass

    class DyingAdapter(FakeSlackAdapter):
        def post(self, message: dict[str, Any], delivery_key: str) -> Any:
            super().post(message, delivery_key)
            raise SimulatedDeath()

    control.actions.adapter = DyingAdapter()
    identifier = control.notify(control.queue.enqueue(TaskCreate("FAKE")), Notice.FAILED)
    with pytest.raises(SimulatedDeath):
        control.actions.deliver_one()
    restarted = ExternalActionService(
        control.database, SETTINGS, clock=FrozenClock(datetime(2026, 9, 5, 13, tzinfo=UTC))
    )
    assert restarted.recover() == 1
    assert not restarted.deliver_one()
    with control.database.transaction() as session:
        action = session.get(ExternalAction, identifier)
        assert action and action.action_status == "UNKNOWN_RESULT"


def test_answer_credentials_rejected_before_persistence(control: SlackControlPlane) -> None:
    identifier = control.queue.enqueue(TaskCreate("FAKE"))
    lease = control.queue.claim(["FAKE"])
    assert lease
    control.queue.complete(lease, {"interrupts": {"fixture": "question"}}, waiting=True)
    action = control.ask_missing(identifier, "fixture", Question.MISSING_INFORMATION)
    control.actions.deliver_one()
    body = interaction(control, action, decision="answer")
    body["state"] = {
        "values": {"jhm_answer_block": {"jhm_answer_input": {"value": "password=secret"}}}
    }
    with pytest.raises(SlackInputError, match="local_entry"):
        control.receive(body)
    assert control.queue.get(identifier).task_status == "WAITING_HUMAN"


def test_web_adapter_uses_mocked_sdk_and_stable_delivery_key(
    control: SlackControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from slack_sdk import WebClient
    from slack_sdk.web.slack_response import SlackResponse

    from job_hunting_machine.slack.adapter import _SlackWebAdapter

    client = WebClient(token="synthetic", retry_handlers=[])
    sent: list[dict[str, Any]] = []

    def post(**kwargs: Any) -> SlackResponse:
        sent.append(kwargs)
        return SlackResponse(
            client=client,
            http_verb="POST",
            api_url="https://slack.com/api/chat.postMessage",
            req_args={},
            data={"ok": True, "channel": "CTEST", "ts": "1700000000.123456"},
            headers={},
            status_code=200,
        )

    monkeypatch.setattr(client, "chat_postMessage", post)
    control.actions.adapter = _SlackWebAdapter(client)
    control.notify(control.queue.enqueue(TaskCreate("FAKE")), Notice.FAILED)
    assert control.actions.deliver_one()
    assert len(sent) == 1 and len(sent[0]["client_msg_id"]) == 36
    assert sent[0]["unfurl_links"] is False
