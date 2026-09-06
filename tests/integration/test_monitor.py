"""Phase 11 monitor acceptance scenarios; every external source is fake."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    AgentTask,
    ApplicationDetails,
    ApplicationPipeline,
    ExternalAction,
    MonitorEvent,
)
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationRepository,
    JobCreate,
    JobRepository,
    TaskCreate,
    TaskRepository,
)
from job_hunting_machine.models import MockResponsesClient, ModelGateway, ModelResponse, TokenUsage
from job_hunting_machine.monitor.classification import KeywordStatusClassifier, LunaStatusClassifier
from job_hunting_machine.monitor.config import MonitorSettings
from job_hunting_machine.monitor.evidence import MonitorEvidenceStore
from job_hunting_machine.monitor.gmail import FakeGmailReader
from job_hunting_machine.monitor.portal import FakePortalReader
from job_hunting_machine.monitor.scheduler import MonitorScheduler
from job_hunting_machine.monitor.service import MonitorService
from job_hunting_machine.monitor.types import (
    ApplicationStatus,
    GmailMessage,
    PortalPage,
    StatusClassification,
    StatusClassifier,
)
from job_hunting_machine.monitor.worker import MonitorWorker
from job_hunting_machine.orchestration.queue import QueueService, RetryableError
from job_hunting_machine.slack.config import SlackSettings
from job_hunting_machine.slack.control import SlackControlPlane

CLOCK = FrozenClock(datetime(2026, 9, 6, 12, tzinfo=UTC))


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    value = Database(tmp_path / "monitor.db")
    value.migrate()
    try:
        yield value
    finally:
        value.dispose()


def scenario(
    database: Database, *, company: str = "Acme", status: str = "SUBMITTED"
) -> tuple[str, str]:
    with database.transaction() as session:
        job = JobRepository(session, CLOCK).create(
            JobCreate(
                f"https://jobs.example.test/{company.casefold()}",
                f"https://jobs.example.test/{company.casefold()}",
                "TEST",
                qualification_status="PASSED",
            )
        )
        created = ApplicationRepository(session, CLOCK).create_from_passed_job(
            job.job_id,
            ApplicationCreate(
                company,
                "Software Engineer",
                job.canonical_url,
                application_url=f"https://portal.example.test/{company.casefold()}",
                ats_type="GREENHOUSE",
            ),
        )
        build = session.get(AgentTask, created.task_id)
        app = session.get(ApplicationPipeline, created.application_id)
        details = session.get(ApplicationDetails, created.application_id)
        assert build and app and details
        build.task_status = "SUCCEEDED"
        app.pipeline_stage = "MONITORING"
        app.application_status = details.application_status = status
        task = TaskRepository(session, CLOCK).create(
            TaskCreate(
                "MONITOR_APPLICATION",
                task_status="READY",
                application_id=app.application_id,
                job_id=job.job_id,
                dedupe_key=f"monitor-test:{app.application_id}",
                payload={"application_id": app.application_id},
            )
        )
        return app.application_id, task.task_id


def service(
    database: Database, tmp_path: Path, classifier: StatusClassifier | None = None
) -> MonitorService:
    return MonitorService(
        database,
        classifier or KeywordStatusClassifier(),
        MonitorSettings(),
        evidence=MonitorEvidenceStore(tmp_path / "monitor-evidence"),
    )


def message(identifier: str, text: str, *, company: str = "Acme") -> GmailMessage:
    return GmailMessage(
        identifier,
        f"Recruiting <jobs@{company.casefold()}.com>",
        f"{company} application update",
        text,
        "2026-09-06T12:00:00Z",
    )


def statuses(database: Database, application_id: str) -> tuple[str, str]:
    with database.transaction() as session:
        app = session.get(ApplicationPipeline, application_id)
        details = session.get(ApplicationDetails, application_id)
        assert app and details
        return app.application_status, details.application_status


def event_count(database: Database) -> int:
    with database.transaction() as session:
        return session.scalar(select(func.count()).select_from(MonitorEvent)) or 0


def test_rejection_email_updates_correct_application(database: Database, tmp_path: Path) -> None:
    acme, task = scenario(database)
    other, _ = scenario(database, company="Beta")
    result = asyncio.run(
        service(database, tmp_path).email(
            task,
            acme,
            message("gmail-reject", "We regret to inform you that we are not moving forward."),
        )
    )
    assert result and result.changed
    assert statuses(database, acme) == ("REJECTED", "REJECTED")
    assert statuses(database, other) == ("SUBMITTED", "SUBMITTED")


def test_interview_email_updates_status(database: Database, tmp_path: Path) -> None:
    application_id, task = scenario(database, status="UNDER_REVIEW")
    result = asyncio.run(
        service(database, tmp_path).email(
            task,
            application_id,
            message("gmail-interview", "We invite you to interview next week."),
        )
    )
    assert result and result.changed
    assert statuses(database, application_id) == ("INTERVIEW", "INTERVIEW")


def test_unrelated_company_email_is_ignored(database: Database, tmp_path: Path) -> None:
    application_id, task = scenario(database)
    unrelated = message(
        "gmail-other", "We regret to inform you that we are not moving forward.", company="Beta"
    )
    assert asyncio.run(service(database, tmp_path).email(task, application_id, unrelated)) is None
    assert event_count(database) == 0
    assert statuses(database, application_id) == ("SUBMITTED", "SUBMITTED")


def test_unchanged_portal_is_audited_without_notification(
    database: Database, tmp_path: Path
) -> None:
    _, task = scenario(database, status="UNDER_REVIEW")
    queue = QueueService(database, clock=CLOCK)
    gmail = FakeGmailReader()
    url = "https://portal.example.test/acme"
    portal = FakePortalReader({url: PortalPage(url, 200, "Your application is under review")})
    slack = SlackControlPlane(
        database,
        SlackSettings(
            team_id="TTEST",
            app_id="ATEST",
            authorized_user_ids=frozenset({"UTEST"}),
            allowed_channel_ids=frozenset({"CTEST"}),
            notification_channel_id="CTEST",
        ),
        clock=CLOCK,
    )
    worker = MonitorWorker(
        queue,
        service(database, tmp_path),
        gmail,
        portal,
        MonitorSettings(),
        slack=slack,
    )
    assert asyncio.run(worker.run_once())
    with database.transaction() as session:
        event = session.scalar(select(MonitorEvent))
        assert event and event.meaningful_change == 0
        assert session.scalar(select(func.count()).select_from(ExternalAction)) == 0
    assert queue.get(task).task_status == "SUCCEEDED"


def test_portal_rejection_updates_state(database: Database, tmp_path: Path) -> None:
    application_id, task = scenario(database)
    page = PortalPage(
        "https://portal.example.test/acme",
        200,
        "We regret to inform you that your application was unsuccessful.",
        "GREENHOUSE",
    )
    result = asyncio.run(service(database, tmp_path).portal(task, application_id, page))
    assert result.changed
    assert statuses(database, application_id) == ("REJECTED", "REJECTED")


@pytest.mark.parametrize("terminal", ["REJECTED", "WITHDRAWN", "CANCELED", "CLOSED"])
def test_terminal_application_is_never_scheduled(database: Database, terminal: str) -> None:
    application_id, task = scenario(database, status=terminal)
    with database.transaction() as session:
        existing = session.get(AgentTask, task)
        assert existing
        existing.task_status = "SUCCEEDED"
    assert MonitorScheduler(database, MonitorSettings(), clock=CLOCK).schedule_due() == []
    with database.transaction() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AgentTask)
            .where(
                AgentTask.application_id == application_id,
                AgentTask.task_type == "MONITOR_APPLICATION",
                AgentTask.task_status == "READY",
            )
        )
        assert count == 0


def test_duplicate_email_does_not_duplicate_event(database: Database, tmp_path: Path) -> None:
    application_id, task = scenario(database)
    item = message("gmail-one", "We are reviewing your application; it is under review.")
    first = asyncio.run(service(database, tmp_path).email(task, application_id, item))
    second = asyncio.run(service(database, tmp_path).email(task, application_id, item))
    assert first and first.changed
    assert second and second.duplicate
    assert event_count(database) == 1


@dataclass
class LowConfidenceClassifier:
    async def classify_email(
        self, task_id: str, application_id: str, item: GmailMessage
    ) -> StatusClassification:
        return StatusClassification(
            detected_status=ApplicationStatus.REJECTED,
            confidence=0.40,
            evidence_codes=("ambiguous",),
        )

    async def classify_portal(
        self, task_id: str, application_id: str, page: PortalPage
    ) -> StatusClassification:
        return StatusClassification(
            detected_status=ApplicationStatus.REJECTED,
            confidence=0.40,
            evidence_codes=("ambiguous",),
        )


def test_low_confidence_model_cannot_make_destructive_transition(
    database: Database, tmp_path: Path
) -> None:
    application_id, task = scenario(database)
    result = asyncio.run(
        service(database, tmp_path, LowConfidenceClassifier()).email(
            task,
            application_id,
            message("gmail-low", "There is an ambiguous update to your application."),
        )
    )
    assert result and not result.changed
    assert statuses(database, application_id) == ("SUBMITTED", "SUBMITTED")
    with database.transaction() as session:
        event = session.scalar(select(MonitorEvent))
        assert event and event.detected_status == "REJECTED" and event.meaningful_change == 0


def test_scheduler_selects_due_active_application_with_run_limit(database: Database) -> None:
    first, first_task = scenario(database, company="Acme")
    second, second_task = scenario(database, company="Beta")
    with database.transaction() as session:
        for task_id in (first_task, second_task):
            task = session.get(AgentTask, task_id)
            assert task
            task.task_status = "SUCCEEDED"
    scheduled = MonitorScheduler(
        database, MonitorSettings(max_portal_checks_per_run=1), clock=CLOCK
    ).schedule_due()
    assert len(scheduled) == 1
    with database.transaction() as session:
        row = session.get(AgentTask, scheduled[0])
        assert row and row.application_id in {first, second}


@dataclass
class TransientGmailReader:
    async def search(self, query: str, *, limit: int) -> list[GmailMessage]:
        raise RetryableError("monitor_fixture_transient")


def test_transient_source_uses_durable_retry_policy(database: Database, tmp_path: Path) -> None:
    _, task = scenario(database)
    queue = QueueService(database, clock=CLOCK)
    worker = MonitorWorker(
        queue,
        service(database, tmp_path),
        TransientGmailReader(),
        FakePortalReader(),
        MonitorSettings(),
    )
    assert asyncio.run(worker.run_once())
    row = queue.get(task)
    assert row.task_status == "WAITING_RETRY"
    assert row.next_run_at is not None


def test_semantic_classifier_is_luna_only(database: Database) -> None:
    application_id, task = scenario(database)
    client = MockResponsesClient(
        [
            ModelResponse(
                '{"detected_status":"UNDER_REVIEW","confidence":0.96,'
                '"evidence_codes":["review_status"]}',
                TokenUsage(100, 0, 20),
                "monitor-response",
            )
        ]
    )
    gateway = ModelGateway(database, client=client, clock=CLOCK)
    result = asyncio.run(
        LunaStatusClassifier(gateway).classify_email(
            task,
            application_id,
            message("gmail-model", "Your status changed without a standard phrase."),
        )
    )
    assert result.detected_status is ApplicationStatus.UNDER_REVIEW
    assert len(client.calls) == 1
    assert client.calls[0].model_id == "gpt-5.6-luna"
