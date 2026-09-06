"""Phase 10 contact, draft, Slack approval, and fake Gmail acceptance."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    AgentTask,
    ApplicationDetails,
    ApplicationPipeline,
    Approval,
    Contact,
    ExternalAction,
    OutreachDraft,
)
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationRepository,
    JobCreate,
    JobRepository,
    TaskCreate,
    TaskRepository,
)
from job_hunting_machine.orchestration.queue import QueueService
from job_hunting_machine.outreach.actions import (
    ConnectorExternalActionService,
    ExternalActionService,
)
from job_hunting_machine.outreach.connector import ConnectorWorker
from job_hunting_machine.outreach.discovery import (
    ContactDiscovery,
    FakeContactReader,
    extract_contacts,
)
from job_hunting_machine.outreach.gmail import FakeGmailAdapter
from job_hunting_machine.outreach.sender import OutreachSender
from job_hunting_machine.outreach.types import PublicPage
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.slack.config import SlackSettings
from job_hunting_machine.slack.control import SlackControlPlane

SOURCE = "https://company.example/team"
USER = "UALLOWED"
SETTINGS = SlackSettings(
    team_id="TTEST",
    app_id="ATEST",
    authorized_user_ids=frozenset({USER}),
    allowed_channel_ids=frozenset({"CTEST"}),
    notification_channel_id="CTEST",
)
HTML = """
<html><body>
  <section data-jhm-contact data-name="Riley Chen" data-title="Technical Recruiter">
    <a href="mailto:Riley.Chen@Company.example">Email</a>
    <a href="https://www.linkedin.com/in/riley-chen">LinkedIn</a>
  </section>
  <section data-jhm-contact data-name="Riley Chen" data-title="Technical Recruiter">
    <a href="mailto:riley.chen@company.example">Email again</a>
  </section>
</body></html>
"""


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    value = Database(tmp_path / "outreach.db")
    value.migrate()
    try:
        yield value
    finally:
        value.dispose()


def setup_connector(database: Database) -> tuple[QueueService, str, str]:
    with database.transaction() as session:
        job = JobRepository(session).create(
            JobCreate(
                "https://company.example/jobs/robotics",
                "https://company.example/jobs/robotics",
                "TEST",
                company_name="Company",
                job_title="Robotics Engineer",
                qualification_status="PASSED",
            )
        )
        created = ApplicationRepository(session).create_from_passed_job(
            job.job_id,
            ApplicationCreate("Company", "Robotics Engineer", job.canonical_url),
        )
        app = session.get(ApplicationPipeline, created.application_id)
        details = session.get(ApplicationDetails, created.application_id)
        prior = session.get(AgentTask, created.task_id)
        assert app and details and prior
        prior.task_status = "SUCCEEDED"
        app.pipeline_stage = "POST_SUBMISSION"
        app.application_status = details.application_status = "SUBMITTED"
        task = TaskRepository(session).create(
            TaskCreate(
                "CONNECT_CONTACTS",
                task_status="READY",
                application_id=created.application_id,
                job_id=job.job_id,
                parent_task_id=prior.task_id,
                dedupe_key=f"connect_contacts:{created.application_id}",
                payload={"application_id": created.application_id, "source_urls": [SOURCE]},
            )
        )
        app.current_task_id = task.task_id
    return QueueService(database), created.application_id, task.task_id


async def run_connector(
    database: Database,
    queue: QueueService,
    gmail: FakeGmailAdapter,
    *,
    slack: SlackControlPlane | None = None,
) -> None:
    actions = ExternalActionService(
        queue,
        gmail,
        runtime_mode=RuntimeMode.STAGING,
        authorized_user_ids=frozenset({USER}),
    )
    worker = ConnectorWorker(
        queue,
        ContactDiscovery(
            FakeContactReader({SOURCE: PublicPage(SOURCE, 200, HTML)}),
            evidence_root=database.path.parent / "evidence",
        ),
        ConnectorExternalActionService(actions),
        slack=slack,
    )
    assert await worker.run_once()


def send_task(database: Database) -> AgentTask:
    with database.transaction() as session:
        row = session.scalar(select(AgentTask).where(AgentTask.task_type == "SEND_OUTREACH_EMAIL"))
        assert row
        return row


def approve_and_resume(database: Database, queue: QueueService, task: AgentTask) -> str:
    memory = queue.memory(task.task_id)
    interrupts = memory.get("interrupts")
    assert isinstance(interrupts, dict) and interrupts
    interrupt_id = next(iter(interrupts))
    with database.transaction() as session:
        approval = session.get(Approval, json.loads(task.payload_json or "{}")["approval_id"])
        assert approval
        approval.approval_status = "APPROVED"
        approval.decided_by_slack_user_id = USER
        approval.decided_at = approval.requested_at
        approval_id = approval.approval_id
    queue.resume(task.task_id, interrupt_id, {"approval_id": approval_id})
    return approval_id


def test_contacts_deduplicate_and_rank_from_public_evidence(database: Database) -> None:
    queue, application_id, _ = setup_connector(database)
    gmail = FakeGmailAdapter()
    asyncio.run(run_connector(database, queue, gmail))
    connector_actions = ConnectorExternalActionService(
        ExternalActionService(
            queue,
            gmail,
            runtime_mode=RuntimeMode.STAGING,
            authorized_user_ids=frozenset({USER}),
        )
    )
    assert not hasattr(connector_actions, "send")
    with database.transaction() as session:
        contacts = list(session.scalars(select(Contact)))
        assert len(contacts) == 1
        assert contacts[0].application_id == application_id
        assert contacts[0].email == "riley.chen@company.example"
        assert contacts[0].contact_type == "RECRUITER" and contacts[0].verified
        assert session.scalar(select(func.count()).select_from(OutreachDraft)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(Approval)
                .where(Approval.approval_type == "SEND_EXTERNAL_MESSAGE")
            )
            == 1
        )
    assert list((database.path.parent / "evidence").glob("**/*.html"))
    assert gmail.draft_calls == 1


def test_drafts_use_correct_application_and_job(database: Database) -> None:
    queue, application_id, _ = setup_connector(database)
    asyncio.run(run_connector(database, queue, FakeGmailAdapter()))
    with database.transaction() as session:
        drafts = list(session.scalars(select(OutreachDraft)))
        assert {row.channel for row in drafts} == {"EMAIL", "LINKEDIN_MANUAL"}
        assert all(row.application_id == application_id for row in drafts)
        email = next(row for row in drafts if row.channel == "EMAIL")
        assert email.subject and "Robotics Engineer" in email.subject
        assert "Company" in email.body and "Riley" in email.body


def test_missing_recruiter_details_are_not_invented() -> None:
    values = extract_contacts(
        PublicPage(SOURCE, 200, '<a href="mailto:careers@company.example">Careers</a>')
    )
    assert len(values) == 1
    assert values[0].full_name is None and values[0].title is None
    assert values[0].contact_type == "OTHER"


def test_unapproved_email_cannot_send(database: Database) -> None:
    queue, _, _ = setup_connector(database)
    gmail = FakeGmailAdapter()
    asyncio.run(run_connector(database, queue, gmail))
    task = send_task(database)
    memory = queue.memory(task.task_id)
    interrupts = memory.get("interrupts")
    assert isinstance(interrupts, dict)
    interrupt_id = next(iter(interrupts))
    queue.resume(task.task_id, interrupt_id, {"forced": True})
    actions = ExternalActionService(
        queue,
        gmail,
        runtime_mode=RuntimeMode.LIVE,
        authorized_user_ids=frozenset({USER}),
    )
    assert asyncio.run(OutreachSender(queue, actions).run_once())
    assert gmail.send_calls == 0
    assert queue.get(task.task_id).task_status == "WAITING_HUMAN"


def test_modified_body_invalidates_approval(database: Database) -> None:
    queue, _, _ = setup_connector(database)
    gmail = FakeGmailAdapter()
    asyncio.run(run_connector(database, queue, gmail))
    task = send_task(database)
    approval_id = approve_and_resume(database, queue, task)
    with database.transaction() as session:
        draft = session.scalar(select(OutreachDraft).where(OutreachDraft.channel == "EMAIL"))
        assert draft
        draft.body += " Changed after approval."
    actions = ExternalActionService(
        queue,
        gmail,
        runtime_mode=RuntimeMode.LIVE,
        authorized_user_ids=frozenset({USER}),
    )
    asyncio.run(OutreachSender(queue, actions).run_once())
    with database.transaction() as session:
        approval = session.get(Approval, approval_id)
        assert approval and approval.approval_status == "REVOKED"
    assert gmail.send_calls == 0


def test_approved_email_sends_once_through_fake_gmail(database: Database) -> None:
    queue, _, _ = setup_connector(database)
    gmail = FakeGmailAdapter()
    asyncio.run(run_connector(database, queue, gmail))
    task = send_task(database)
    approve_and_resume(database, queue, task)
    actions = ExternalActionService(
        queue,
        gmail,
        runtime_mode=RuntimeMode.LIVE,
        authorized_user_ids=frozenset({USER}),
    )
    assert asyncio.run(OutreachSender(queue, actions).run_once())
    assert gmail.send_calls == 1
    with database.transaction() as session:
        draft = session.scalar(select(OutreachDraft).where(OutreachDraft.channel == "EMAIL"))
        sent = session.scalar(
            select(ExternalAction).where(ExternalAction.action_type == "SEND_EMAIL")
        )
        assert draft and draft.draft_status == "SENT"
        assert sent and sent.action_status == "SUCCEEDED"


def interaction(control: SlackControlPlane, action_id: str) -> dict[str, Any]:
    with control.database.transaction() as session:
        action = session.get(ExternalAction, action_id)
        assert action
        receipt = json.loads(action.result_json or "{}")["receipt"]
    return {
        "type": "block_actions",
        "team": {"id": "TTEST"},
        "api_app_id": "ATEST",
        "user": {"id": USER},
        "channel": {"id": "CTEST"},
        "container": {
            "type": "message",
            "channel_id": "CTEST",
            "message_ts": receipt["message_ts"],
        },
        "actions": [
            {
                "type": "button",
                "action_id": "jhm_approve",
                "action_ts": "1700000001.000001",
                "value": action_id,
            }
        ],
    }


def test_duplicate_slack_callback_cannot_resend(database: Database) -> None:
    queue, _, _ = setup_connector(database)
    gmail = FakeGmailAdapter()
    control = SlackControlPlane(database, SETTINGS)
    asyncio.run(run_connector(database, queue, gmail, slack=control))
    slack_action = None
    while control.actions.deliver_one():
        pass
    with database.transaction() as session:
        slack_action = session.scalar(
            select(ExternalAction)
            .join(Approval, ExternalAction.approval_id == Approval.approval_id)
            .where(
                ExternalAction.action_type == "SLACK_NOTIFICATION",
                Approval.approval_type == "SEND_EMAIL",
            )
        )
        assert slack_action
        slack_action_id = slack_action.external_action_id
    body = interaction(control, slack_action_id)
    event_id = control.receive(body)
    assert control.process_one()
    actions = ExternalActionService(
        queue,
        gmail,
        runtime_mode=RuntimeMode.LIVE,
        authorized_user_ids=frozenset({USER}),
    )
    assert asyncio.run(OutreachSender(queue, actions).run_once())
    assert control.receive(body) == event_id
    assert not control.process_one()
    assert not asyncio.run(OutreachSender(queue, actions).run_once())
    assert gmail.send_calls == 1
