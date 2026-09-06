"""Phase 9 immutable review, approval, submission, and reconciliation acceptance."""

import asyncio
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select

from job_hunting_machine.browser.config import BrowserSettings
from job_hunting_machine.browser.fake import FAKE_ATS_ORIGIN, FakeATSApplication
from job_hunting_machine.browser.manager import BrowserManager
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    AgentTask,
    ApplicationDetails,
    ApplicationPipeline,
    Approval,
    ExternalAction,
    FormAnswer,
)
from job_hunting_machine.database.repositories import (
    AnswerUpsert,
    ApplicationCreate,
    ApplicationRepository,
    ArtifactCreate,
    ArtifactRepository,
    FormAnswerRepository,
    JobCreate,
    JobRepository,
    TaskCreate,
    TaskRepository,
)
from job_hunting_machine.orchestration.queue import QueueService
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.security.paths import PathGuard
from job_hunting_machine.slack.approvals import ApprovalService
from job_hunting_machine.slack.config import SlackInputError, SlackSettings
from job_hunting_machine.submission.fake import FakeSubmissionAdapter
from job_hunting_machine.submission.playwright_adapter import PlaywrightSubmissionAdapter
from job_hunting_machine.submission.review import ReviewService, canonical_json, read_review
from job_hunting_machine.submission.service import ExternalActionService, SubmissionError
from job_hunting_machine.submission.worker import SubmissionWorker

USER = "UTEST"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "submission.db")
    database.migrate()
    try:
        yield database
    finally:
        database.dispose()


@dataclass(frozen=True)
class Scenario:
    database: Database
    queue: QueueService
    application_id: str
    task_id: str
    approval_id: str
    resume_path: Path

    def approval(self, status: str = "APPROVED", *, user: str = USER) -> None:
        with self.database.transaction() as session:
            row = session.get(Approval, self.approval_id)
            assert row
            row.approval_status = status
            row.decided_by_slack_user_id = user
            row.decided_at = row.requested_at

    def resume(self) -> None:
        memory = self.queue.memory(self.task_id)
        interrupts = memory.get("interrupts")
        assert isinstance(interrupts, dict) and interrupts
        interrupt_id = next(iter(interrupts))
        self.queue.resume(self.task_id, interrupt_id, {"recorded": True})


def setup_scenario(database: Database, tmp_path: Path) -> Scenario:
    resume = PathGuard().write_bytes(tmp_path / "resume.pdf", b"%PDF-1.4\nphase9")
    with database.transaction() as session:
        job = JobRepository(session).create(
            JobCreate(
                "https://example.test/jobs/1",
                "https://example.test/jobs/1",
                "TEST",
                qualification_status="PASSED",
            )
        )
        created = ApplicationRepository(session).create_from_passed_job(
            job.job_id,
            ApplicationCreate(
                "Example Company",
                "Software Engineer",
                job.canonical_url,
                application_url=FAKE_ATS_ORIGIN + "/review",
            ),
        )
        artifact = ArtifactRepository(session).create(
            ArtifactCreate(
                "RESUME_PDF",
                str(resume),
                hashlib.sha256(resume.read_bytes()).hexdigest(),
                "application/pdf",
                application_id=created.application_id,
            )
        )
        details = session.get(ApplicationDetails, created.application_id)
        app = session.get(ApplicationPipeline, created.application_id)
        old_task = session.get(AgentTask, created.task_id)
        assert details and app and old_task
        old_task.task_status = "SUCCEEDED"
        details.resume_artifact_id = artifact.artifact_id
        details.application_status = "READY_TO_REVIEW"
        FormAnswerRepository(session).upsert(
            AnswerUpsert(
                created.application_id,
                "contact",
                "first_name",
                "First name",
                "Ada",
                "USER",
                "slack:event:test",
                "VALIDATED",
                1.0,
            )
        )
        task = TaskRepository(session).create(
            TaskCreate(
                "CREATE_REVIEW",
                task_status="READY",
                application_id=created.application_id,
                job_id=job.job_id,
                parent_task_id=created.task_id,
                dedupe_key=f"create_review:{created.application_id}",
                payload={"application_id": created.application_id},
            )
        )
        app.pipeline_stage = "REVIEW"
        app.application_status = "READY_TO_REVIEW"
        app.current_task_id = task.task_id
    queue = QueueService(database)
    lease = queue.claim(["CREATE_REVIEW"])
    assert lease
    record = ReviewService(queue).prepare(lease)
    payload = read_review(record.path, record.sha256)
    assert hashlib.sha256(canonical_json(payload)).hexdigest() == record.sha256
    return Scenario(
        database,
        queue,
        created.application_id,
        record.task_id,
        record.approval_id,
        resume,
    )


def run_worker(scenario: Scenario, adapter: FakeSubmissionAdapter) -> None:
    worker = SubmissionWorker(
        scenario.queue,
        adapter,
        runtime_mode=RuntimeMode.LIVE,
        authorized_user_ids=frozenset({USER}),
    )
    assert asyncio.run(worker.run_once())


def action(database: Database) -> ExternalAction | None:
    with database.transaction() as session:
        return session.scalar(
            select(ExternalAction).where(ExternalAction.action_type == "SUBMIT_APPLICATION")
        )


def test_no_approval_is_blocked(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.resume()
    adapter = FakeSubmissionAdapter()
    run_worker(scenario, adapter)
    assert adapter.submit_calls == 0 and action(database) is None
    assert scenario.queue.get(scenario.task_id).task_status == "WAITING_HUMAN"


def test_dry_run_cannot_enter_submission_boundary(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    scenario.resume()
    lease = scenario.queue.claim(["SUBMIT_APPLICATION"])
    assert lease
    service = ExternalActionService(
        scenario.queue,
        FakeSubmissionAdapter(),
        runtime_mode=RuntimeMode.DRY_RUN,
        authorized_user_ids=frozenset({USER}),
    )
    with pytest.raises(SubmissionError, match="submission_requires_live_mode"):
        asyncio.run(service.execute(lease))
    assert action(database) is None


def test_rejected_approval_is_blocked(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval("REJECTED")
    scenario.resume()
    adapter = FakeSubmissionAdapter()
    run_worker(scenario, adapter)
    assert adapter.submit_calls == 0 and action(database) is None


def test_expired_approval_is_blocked_and_recorded(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    with database.transaction() as session:
        approval = session.get(Approval, scenario.approval_id)
        assert approval
        approval.valid_until = "2000-01-01T00:00:00.000Z"
    scenario.resume()
    adapter = FakeSubmissionAdapter()
    run_worker(scenario, adapter)
    with database.transaction() as session:
        approval = session.get(Approval, scenario.approval_id)
        assert approval and approval.approval_status == "EXPIRED"
    assert adapter.submit_calls == 0


def test_wrong_slack_user_cannot_approve(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    settings = SlackSettings(authorized_user_ids=frozenset({USER}))
    with database.transaction() as session:
        approval = session.get(Approval, scenario.approval_id)
        assert approval
        with pytest.raises(SlackInputError, match="unauthorized_slack_user"):
            ApprovalService(settings, scenario.queue.clock).decide(
                session, approval, decision="APPROVED", user_id="UWRONG"
            )
    assert scenario.queue.get(scenario.task_id).task_status == "WAITING_HUMAN"


def test_changed_answer_revokes_approval(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    scenario.resume()
    with database.transaction() as session:
        answer = session.scalar(select(FormAnswer))
        assert answer
        answer.answer_json = json.dumps("Grace")
    adapter = FakeSubmissionAdapter()
    run_worker(scenario, adapter)
    with database.transaction() as session:
        approval = session.get(Approval, scenario.approval_id)
        assert approval and approval.approval_status == "REVOKED"
    assert adapter.submit_calls == 0


def test_changed_resume_revokes_approval(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    scenario.resume()
    PathGuard().write_bytes(scenario.resume_path, b"changed")
    adapter = FakeSubmissionAdapter()
    run_worker(scenario, adapter)
    with database.transaction() as session:
        approval = session.get(Approval, scenario.approval_id)
        assert approval and approval.approval_status == "REVOKED"
    assert adapter.submit_calls == 0


def test_valid_approval_submits_once(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    scenario.resume()
    adapter = FakeSubmissionAdapter()
    run_worker(scenario, adapter)
    with database.transaction() as session:
        app = session.get(ApplicationPipeline, scenario.application_id)
        approval = session.get(Approval, scenario.approval_id)
        assert app and app.application_status == "SUBMITTED"
        assert approval and approval.approval_status == "CONSUMED"
    row = action(database)
    assert row and row.action_status == "SUCCEEDED" and adapter.submit_calls == 1


def test_valid_approval_submits_once_against_fake_ats(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    scenario.resume()
    fake = FakeATSApplication()
    manager = BrowserManager(BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=fake)
    worker = SubmissionWorker(
        scenario.queue,
        PlaywrightSubmissionAdapter(manager),
        runtime_mode=RuntimeMode.LIVE,
        authorized_user_ids=frozenset({USER}),
    )

    async def run() -> None:
        assert await worker.run_once()
        await manager.close()

    asyncio.run(run())
    assert fake.requests.count(("POST", "/submitted")) == 1
    with database.transaction() as session:
        app = session.get(ApplicationPipeline, scenario.application_id)
        assert app and app.application_status == "SUBMITTED"


def test_retry_after_success_does_not_resubmit(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    scenario.resume()
    adapter = FakeSubmissionAdapter()
    run_worker(scenario, adapter)
    assert not asyncio.run(
        SubmissionWorker(
            scenario.queue,
            adapter,
            runtime_mode=RuntimeMode.LIVE,
            authorized_user_ids=frozenset({USER}),
        ).run_once()
    )
    assert adapter.submit_calls == 1


def test_lost_response_becomes_unknown(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    scenario.resume()
    adapter = FakeSubmissionAdapter(mode="lost_after_click")
    run_worker(scenario, adapter)
    row = action(database)
    with database.transaction() as session:
        app = session.get(ApplicationPipeline, scenario.application_id)
        assert app and app.application_status == "SUBMISSION_UNKNOWN"
    assert row and row.action_status == "UNKNOWN_RESULT"


def test_reconciliation_finds_confirmation(database: Database, tmp_path: Path) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    scenario.resume()
    adapter = FakeSubmissionAdapter(mode="lost_after_click")
    run_worker(scenario, adapter)
    scenario.resume()
    run_worker(scenario, adapter)
    with database.transaction() as session:
        app = session.get(ApplicationPipeline, scenario.application_id)
        assert app and app.application_status == "SUBMITTED"
    assert adapter.submit_calls == 1 and adapter.reconcile_calls == 1


def test_reconciliation_finds_no_submission_and_reviews_again(
    database: Database, tmp_path: Path
) -> None:
    scenario = setup_scenario(database, tmp_path)
    scenario.approval()
    scenario.resume()
    adapter = FakeSubmissionAdapter(mode="unknown_without_submit")
    run_worker(scenario, adapter)
    scenario.resume()
    run_worker(scenario, adapter)
    with database.transaction() as session:
        app = session.get(ApplicationPipeline, scenario.application_id)
        row = action(database)
        assert app
        review = session.scalar(
            select(AgentTask).where(
                AgentTask.task_id == app.current_task_id,
                AgentTask.task_type == "CREATE_REVIEW",
            )
        )
        assert app.application_status == "READY_TO_REVIEW"
        assert row and row.action_status == "FAILED"
        assert review and review.task_status == "READY"
    assert adapter.submit_calls == 1 and adapter.reconcile_calls == 1
