"""Phase 8 acceptance against a real Chromium process and a routed fake ATS."""

import asyncio
import hashlib
import inspect
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from job_hunting_machine.browser import BaseAdapter
from job_hunting_machine.browser.adapters import ADAPTERS
from job_hunting_machine.browser.artifacts import application_artifacts
from job_hunting_machine.browser.config import BrowserSettings, browser_settings
from job_hunting_machine.browser.detector import adapter_for, detect_ats
from job_hunting_machine.browser.fake import FAKE_ATS_ORIGIN, FakeATSApplication
from job_hunting_machine.browser.manager import BrowserManager
from job_hunting_machine.browser.worker import FormWorker
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    AgentTask,
    ApplicationDetails,
    ApplicationPipeline,
    Approval,
    Artifact,
    BrowserSession,
    ExternalAction,
    FormAnswer,
)
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationRepository,
    ArtifactCreate,
    ArtifactRepository,
    FormInformationRepository,
    InformationUpsert,
    JobCreate,
    JobRepository,
    TaskCreate,
    TaskRepository,
)
from job_hunting_machine.orchestration.queue import QueueService
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "form.db")
    database.migrate()
    try:
        yield database
    finally:
        database.dispose()


def setup_form(database: Database, tmp_path: Path) -> tuple[QueueService, str, str]:
    resume = PathGuard().write_bytes(tmp_path / "resume.pdf", b"%PDF-1.4\nfixture")
    with database.transaction() as session:
        job = JobRepository(session).create(
            JobCreate(
                FAKE_ATS_ORIGIN + "/apply",
                FAKE_ATS_ORIGIN + "/apply",
                "TEST",
                qualification_status="PASSED",
            )
        )
        created = ApplicationRepository(session).create_from_passed_job(
            job.job_id,
            ApplicationCreate(
                "Fixture Company",
                "Fixture Role",
                job.canonical_url,
                application_url=FAKE_ATS_ORIGIN + "/apply",
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
        pipeline = session.get(ApplicationPipeline, created.application_id)
        resume_task = session.get(AgentTask, created.task_id)
        assert details and pipeline and resume_task
        resume_task.task_status = "SUCCEEDED"
        details.resume_artifact_id = artifact.artifact_id
        details.application_status = "FORM_READY"
        form_task = TaskRepository(session).create(
            TaskCreate(
                "FORM_PROCESS",
                task_status="READY",
                application_id=created.application_id,
                job_id=job.job_id,
                parent_task_id=created.task_id,
                dedupe_key=f"form_process:{created.application_id}",
                payload={"application_id": created.application_id},
            )
        )
        pipeline.pipeline_stage = "FORM"
        pipeline.application_status = "FORM_READY"
        pipeline.current_task_id = form_task.task_id
        repository = FormInformationRepository(session)
        for key, value in (
            ("identity.first_name", "Ada"),
            ("identity.last_name", "Lovelace"),
        ):
            repository.upsert(
                InformationUpsert(
                    key,
                    "identity",
                    value,
                    "string",
                    "NORMAL",
                    "ALLOW",
                    "fixture:verified",
                    True,
                )
            )
    return QueueService(database), created.application_id, form_task.task_id


def test_form_selects_only_application_pdf_not_tex_or_legacy_docx(
    database: Database, tmp_path: Path
) -> None:
    _, application_id, _ = setup_form(database, tmp_path)
    tex = PathGuard().write_text(tmp_path / "resume.tex", "local latex")
    with database.transaction() as session:
        tex_artifact = ArtifactRepository(session).create(
            ArtifactCreate(
                "RESUME_TEX",
                str(tex),
                hashlib.sha256(tex.read_bytes()).hexdigest(),
                "application/x-tex",
                application_id=application_id,
            )
        )
        details = session.get(ApplicationDetails, application_id)
        assert details
        pdf_id = details.resume_artifact_id
        selected = application_artifacts(session, application_id)
        assert selected["resume"].artifact_id == pdf_id
        details.resume_artifact_id = tex_artifact.artifact_id
        with pytest.raises(ValueError, match="artifact_application_or_type_mismatch"):
            application_artifacts(session, application_id)


def approve_and_resume(database: Database, queue: QueueService, task_id: str) -> str:
    memory = queue.memory(task_id)
    interrupts = memory["interrupts"]
    assert isinstance(interrupts, dict)
    interrupt_id, item = next(iter(interrupts.items()))
    assert isinstance(item, dict)
    approval_id = item["approval_id"]
    assert isinstance(approval_id, str)
    with database.transaction() as session:
        approval = session.get(Approval, approval_id)
        assert approval
        approval.approval_status = "APPROVED"
        approval.decided_by_slack_user_id = "UFIXTURE"
        approval.decided_at = approval.requested_at
    queue.resume(
        task_id,
        interrupt_id,
        {"approval_id": approval_id, "decision": "APPROVED", "user_id": "UFIXTURE"},
    )
    return approval_id


def test_fake_ats_completes_at_review_without_submission(
    database: Database, tmp_path: Path
) -> None:
    queue, application_id, task_id = setup_form(database, tmp_path)
    fake = FakeATSApplication()
    manager = BrowserManager(BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=fake)
    worker = FormWorker(queue, manager)

    async def run() -> None:
        await worker.startup()
        assert await worker.run_once()
        assert queue.get(task_id).task_status == "WAITING_HUMAN"
        approve_and_resume(database, queue, task_id)
        assert await worker.run_once()
        await worker.close()

    asyncio.run(run())
    with database.transaction() as session:
        pipeline = session.get(ApplicationPipeline, application_id)
        assert pipeline
        assert pipeline.application_status == "READY_TO_REVIEW"
        assert pipeline.pipeline_stage == "REVIEW"
        assert queue.get(task_id).task_status == "SUCCEEDED"
        answers = list(session.scalars(select(FormAnswer)))
        assert {(row.field_key, row.answer_status) for row in answers} == {
            ("first_name", "VALIDATED"),
            ("last_name", "VALIDATED"),
            ("resume", "VALIDATED"),
        }
        assert session.scalar(select(func.count()).select_from(ExternalAction)) == 3
    assert ("POST", "/submitted") not in fake.requests


def test_single_page_form_is_filled_but_submit_control_is_not_activated(
    database: Database, tmp_path: Path
) -> None:
    queue, application_id, task_id = setup_form(database, tmp_path)
    fake = FakeATSApplication(single_page_submit=True)
    worker = FormWorker(
        queue,
        BrowserManager(BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=fake),
    )

    async def run() -> None:
        await worker.run_once()
        approve_and_resume(database, queue, task_id)
        await worker.run_once()
        await worker.close()

    asyncio.run(run())
    assert queue.get(task_id).task_status == "SUCCEEDED"
    with database.transaction() as session:
        app = session.get(ApplicationPipeline, application_id)
        assert app and app.application_status == "READY_TO_REVIEW"
        assert session.scalar(select(func.count()).select_from(ExternalAction)) == 3
    assert ("POST", "/submitted") not in fake.requests


def test_unknown_field_pauses_before_any_browser_mutation(
    database: Database, tmp_path: Path
) -> None:
    queue, application_id, task_id = setup_form(database, tmp_path)
    fake = FakeATSApplication(extra_field=True)
    worker = FormWorker(
        queue,
        BrowserManager(BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=fake),
    )

    async def run() -> None:
        await worker.run_once()
        await worker.close()

    asyncio.run(run())
    assert queue.get(task_id).task_status == "WAITING_HUMAN"
    with database.transaction() as session:
        pipeline = session.get(ApplicationPipeline, application_id)
        assert pipeline and pipeline.application_status == "WAITING_USER_INPUT"
        assert session.scalar(select(func.count()).select_from(ExternalAction)) == 0


def test_user_answer_resumes_exact_task_with_provenance(database: Database, tmp_path: Path) -> None:
    queue, application_id, task_id = setup_form(database, tmp_path)
    worker = FormWorker(
        queue,
        BrowserManager(
            BrowserSettings(runtime_mode=RuntimeMode.STAGING),
            fake=FakeATSApplication(extra_field=True),
        ),
    )

    async def run() -> None:
        await worker.run_once()
        memory = queue.memory(task_id)
        interrupts = memory["interrupts"]
        assert isinstance(interrupts, dict)
        interrupt_id = next(iter(interrupts))
        queue.resume(
            task_id,
            interrupt_id,
            {"answer": "Rover", "user_id": "UALLOWED", "event_id": "EvAnswer"},
        )
        await worker.run_once()
        await worker.close()

    asyncio.run(run())
    assert queue.get(task_id).task_status == "WAITING_HUMAN"
    with database.transaction() as session:
        answer = session.scalar(
            select(FormAnswer).where(
                FormAnswer.application_id == application_id,
                FormAnswer.field_key == "favorite_robot",
            )
        )
        assert answer and json.loads(answer.answer_json or "null") == "Rover"
        assert answer.answer_source_type == "USER"
        assert answer.answer_source_reference == "slack:EvAnswer:UALLOWED"
        assert session.scalar(select(func.count()).select_from(ExternalAction)) == 0


def test_rejected_prepare_approval_blocks_all_mutation(database: Database, tmp_path: Path) -> None:
    queue, application_id, task_id = setup_form(database, tmp_path)
    worker = FormWorker(
        queue,
        BrowserManager(
            BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=FakeATSApplication()
        ),
    )

    async def run() -> None:
        await worker.run_once()
        memory = queue.memory(task_id)
        interrupts = memory["interrupts"]
        assert isinstance(interrupts, dict)
        interrupt_id, item = next(iter(interrupts.items()))
        assert isinstance(item, dict)
        approval_id = item["approval_id"]
        assert isinstance(approval_id, str)
        with database.transaction() as session:
            approval = session.get(Approval, approval_id)
            assert approval
            approval.approval_status = "REJECTED"
        queue.resume(task_id, interrupt_id, {"decision": "REJECTED"})
        await worker.run_once()
        await worker.close()

    asyncio.run(run())
    assert queue.get(task_id).task_status == "WAITING_HUMAN"
    with database.transaction() as session:
        app = session.get(ApplicationPipeline, application_id)
        assert app and app.application_status == "FORM_READY"
        assert session.scalar(select(func.count()).select_from(ExternalAction)) == 0


@pytest.mark.parametrize("challenge", ["captcha", "mfa"])
def test_challenges_pause_without_interaction(
    database: Database, tmp_path: Path, challenge: str
) -> None:
    queue, application_id, task_id = setup_form(database, tmp_path)
    fake = FakeATSApplication(captcha=challenge == "captcha", mfa=challenge == "mfa")
    worker = FormWorker(
        queue,
        BrowserManager(BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=fake),
    )

    async def run() -> None:
        await worker.run_once()
        await worker.close()

    asyncio.run(run())
    assert queue.get(task_id).task_status == "WAITING_HUMAN"
    memory = queue.memory(task_id)
    interrupts = memory["interrupts"]
    assert isinstance(interrupts, dict)
    item = next(iter(interrupts.values()))
    assert isinstance(item, dict)
    assert challenge.upper() in item["challenges"]
    with database.transaction() as session:
        pipeline = session.get(ApplicationPipeline, application_id)
        assert pipeline and pipeline.application_status == "WAITING_USER_INPUT"
        assert session.scalar(select(func.count()).select_from(ExternalAction)) == 0


@pytest.mark.parametrize(
    ("marker", "expected", "supported"),
    [
        ("GREENHOUSE", "GREENHOUSE", True),
        ("LEVER", "LEVER", True),
        ("ASHBY", "ASHBY", True),
        ("GENERIC", "GENERIC", True),
        ("WORKDAY", "WORKDAY", False),
        ("ICIMS", "ICIMS", False),
        ("SMARTRECRUITERS", "SMARTRECRUITERS", False),
    ],
)
def test_ats_detection_and_support(marker: str, expected: str, supported: bool) -> None:
    async def run() -> None:
        fake = FakeATSApplication(ats=marker)
        manager = BrowserManager(BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=fake)
        application_id = "APP_01K4EZ1M00" + "0" * 16
        async with manager.session(application_id, FAKE_ATS_ORIGIN + "/apply") as handle:
            assert (await detect_ats(handle.page)).value == expected
            assert (await adapter_for(handle.page)).supported_for_mutation is supported
        await manager.close()

    asyncio.run(run())


def test_storage_state_is_private_root_confined_and_survives_restart(tmp_path: Path) -> None:
    application_id = "APP_01K4EZ1M00" + "0" * 16

    async def run() -> None:
        fake = FakeATSApplication()
        settings = BrowserSettings(runtime_mode=RuntimeMode.STAGING)
        first = BrowserManager(settings, fake=fake)
        async with first.session(application_id, FAKE_ATS_ORIGIN + "/apply") as handle:
            await handle.page.evaluate("localStorage.setItem('fixture','saved')")
            state_path = await first.save_state(handle)
        await first.close()
        assert state_path.is_relative_to(PROJECT_ROOT)
        assert state_path.stat().st_mode & 0o777 == 0o600
        second = BrowserManager(settings, fake=fake)
        async with second.session(application_id, FAKE_ATS_ORIGIN + "/apply") as handle:
            assert await handle.page.evaluate("localStorage.getItem('fixture')") == "saved"
        await second.close()

    asyncio.run(run())


def test_second_same_domain_session_is_rejected() -> None:
    application_id = "APP_01K4EZ1M00" + "0" * 16

    async def run() -> None:
        settings = BrowserSettings(runtime_mode=RuntimeMode.STAGING)
        first = BrowserManager(settings, fake=FakeATSApplication())
        second = BrowserManager(settings, fake=FakeATSApplication())
        async with first.session(application_id, FAKE_ATS_ORIGIN + "/apply"):
            with pytest.raises(ValueError, match="domain_already"):
                async with second.session(application_id, FAKE_ATS_ORIGIN + "/apply"):
                    pass
        await first.close()
        await second.close()

    asyncio.run(run())


def test_no_submit_callable_exists() -> None:
    assert "submit" not in BaseAdapter.__annotations__
    for adapter in ADAPTERS.values():
        assert not hasattr(adapter, "submit")
    assert not hasattr(FormWorker, "submit")
    source = inspect.getsource(FormWorker)
    assert ".click(" not in source


def test_live_browser_requires_all_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORM_BROWSER_ALLOW_LIVE", "1")
    assert browser_settings(RuntimeMode.LIVE, live_flag=True).allow_live
    with pytest.raises(ValueError, match="opt_ins"):
        browser_settings(RuntimeMode.LIVE, live_flag=False)
    with pytest.raises(ValueError, match="live_runtime"):
        browser_settings(RuntimeMode.DRY_RUN, live_flag=True)


def test_corrupt_resume_fails_before_business_state_change(
    database: Database, tmp_path: Path
) -> None:
    queue, application_id, task_id = setup_form(database, tmp_path)
    with database.transaction() as session:
        artifact = session.scalar(select(Artifact).where(Artifact.application_id == application_id))
        assert artifact
        PathGuard().write_bytes(PROJECT_ROOT / artifact.path, b"changed")
    worker = FormWorker(
        queue,
        BrowserManager(
            BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=FakeATSApplication()
        ),
    )

    async def run() -> None:
        await worker.run_once()
        await worker.close()

    asyncio.run(run())
    assert queue.get(task_id).task_status == "FAILED"
    with database.transaction() as session:
        pipeline = session.get(ApplicationPipeline, application_id)
        assert pipeline and pipeline.application_status == "FORM_READY"
        assert session.scalar(select(func.count()).select_from(BrowserSession)) == 0


def test_expired_browser_login_pauses_before_browser_access(
    database: Database, tmp_path: Path
) -> None:
    from job_hunting_machine.database.repositories import (
        BrowserSessionRepository,
        SessionCheckpoint,
    )

    queue, application_id, task_id = setup_form(database, tmp_path)
    state = PathGuard().write_text(tmp_path / "expired-storage.json", '{"cookies":[],"origins":[]}')
    with database.transaction() as session:
        row = BrowserSessionRepository(session).checkpoint(
            SessionCheckpoint(
                application_id,
                "GREENHOUSE",
                str(state),
                FAKE_ATS_ORIGIN + "/apply",
                "contact",
                "EXPIRED",
            )
        )
        row.expires_at = "2000-01-01T00:00:00.000Z"
    fake = FakeATSApplication()
    manager = BrowserManager(BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=fake)
    worker = FormWorker(queue, manager)
    asyncio.run(worker.run_once())
    assert queue.get(task_id).task_status == "WAITING_HUMAN"
    assert fake.requests == []
    memory = queue.memory(task_id)
    interrupts = memory["interrupts"]
    assert isinstance(interrupts, dict)
    assert any(
        isinstance(item, dict) and item.get("kind") == "EXPIRED_BROWSER_LOGIN"
        for item in interrupts.values()
    )


def test_missing_transcript_pauses_before_any_upload(database: Database, tmp_path: Path) -> None:
    queue, _, task_id = setup_form(database, tmp_path)
    fake = FakeATSApplication(transcript=True)
    manager = BrowserManager(BrowserSettings(runtime_mode=RuntimeMode.STAGING), fake=fake)
    worker = FormWorker(queue, manager)

    async def run() -> None:
        assert await worker.run_once()
        await worker.close()

    asyncio.run(run())
    assert queue.get(task_id).task_status == "WAITING_HUMAN"
    with database.transaction() as session:
        transcript = session.scalar(select(FormAnswer).where(FormAnswer.field_key == "transcript"))
        assert transcript and transcript.answer_status == "NEEDS_USER"
        assert session.scalar(select(func.count()).select_from(ExternalAction)) == 0


def test_browser_process_crash_can_restart_cleanly(tmp_path: Path) -> None:
    manager = BrowserManager(
        BrowserSettings(runtime_mode=RuntimeMode.STAGING),
        fake=FakeATSApplication(),
        root=tmp_path / "sessions",
    )

    async def run() -> None:
        await manager.start()
        first = manager._browser
        assert first and first.is_connected()
        await first.close()
        assert not first.is_connected()
        await manager.start()
        assert manager._browser and manager._browser is not first
        assert manager._browser.is_connected()
        await manager.close()

    asyncio.run(run())
