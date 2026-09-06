"""Phase 12 operator controls, fault-matrix coverage, and fail-closed diagnostics."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from job_hunting_machine.cli import app
from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import AgentTask, ApplicationDetails
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationRepository,
    JobCreate,
    JobRepository,
    TaskCreate,
)
from job_hunting_machine.orchestration.queue import QueueService
from job_hunting_machine.reliability.backup import BackupService
from job_hunting_machine.reliability.doctor import Doctor
from job_hunting_machine.reliability.evals import evaluate_all
from job_hunting_machine.reliability.recovery import RecoveryError, RecoveryService
from job_hunting_machine.reliability.reports import OperationalReports
from job_hunting_machine.resume.config import ResumePolicy
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.security.paths import PROJECT_ROOT
from job_hunting_machine.slack.config import SlackSettings

CLOCK = FrozenClock(datetime(2026, 9, 6, 12, tzinfo=UTC))
runner = CliRunner()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    value = Database(tmp_path / "phase12.db")
    value.migrate()
    try:
        yield value
    finally:
        value.dispose()


def application(database: Database) -> tuple[str, str]:
    with database.transaction() as session:
        job = JobRepository(session, CLOCK).create(
            JobCreate(
                "https://example.test/phase12",
                "https://example.test/phase12",
                "TEST",
                qualification_status="PASSED",
            )
        )
        created = ApplicationRepository(session, CLOCK).create_from_passed_job(
            job.job_id,
            ApplicationCreate("Fixture", "Engineer", job.canonical_url),
        )
        return created.application_id, created.task_id


def test_fault_matrix_names_every_required_scenario() -> None:
    path = PROJECT_ROOT / "config/fault-injection.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = value["scenarios"]
    required = {
        "laptop_process_termination",
        "stale_worker_lease",
        "sqlite_busy_locked",
        "openai_timeout",
        "openai_malformed_structured_output",
        "slack_duplicate_delivery",
        "slack_unavailable",
        "browser_crash",
        "expired_browser_login",
        "captcha",
        "gmail_unavailable",
        "network_loss_before_submit",
        "network_loss_immediately_after_submit",
        "duplicate_approval_callback",
        "changed_payload_after_approval",
        "corrupted_artifact",
        "missing_source_resume",
        "missing_transcript",
        "duplicate_job",
        "deleted_job_posting",
        "application_status_conflict",
    }
    assert value["version"] == 1
    assert {row["name"] for row in rows} == required
    for row in rows:
        filename, test_name = row["test"].split("::", 1)
        source = (PROJECT_ROOT / filename).read_text(encoding="utf-8")
        assert f"def {test_name.split('[', 1)[0]}(" in source


def test_online_backup_is_integral_and_preserves_committed_state(
    database: Database, tmp_path: Path
) -> None:
    task_id = QueueService(database, clock=CLOCK).enqueue(TaskCreate("FAKE"))
    result = BackupService(root=tmp_path / "backups", clock=CLOCK).create(database.path)
    manifest = json.loads(Path(result.manifest).read_text(encoding="utf-8"))
    backup = Path(result.directory) / "data/job-hunting.db"
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT task_status FROM agent_queue WHERE task_id = ?", (task_id,)
        ).fetchone() == ("READY",)
    assert manifest["retention_recommendation_days"] == 30
    assert any(item["path"] == "data/job-hunting.db" for item in manifest["files"])


def test_recovery_repairs_only_stale_work_and_preserves_human_wait(
    database: Database,
) -> None:
    old = FrozenClock(datetime(2000, 1, 1, tzinfo=UTC))
    queue = QueueService(database, clock=old, lease_seconds=1)
    stale = queue.enqueue(TaskCreate("FAKE"))
    lease = queue.claim(["FAKE"])
    assert lease and lease.task_id == stale
    waiting = queue.enqueue(TaskCreate("FAKE_HUMAN"))
    waiting_lease = queue.claim(["FAKE_HUMAN"])
    assert waiting_lease
    queue.complete(waiting_lease, {"interrupts": {"fixture": {}}}, waiting=True)
    result = RecoveryService(database, slack_settings=SlackSettings()).recover()
    assert result.queue_tasks == 1
    assert QueueService(database).get(stale).task_status == "READY"
    assert QueueService(database).get(waiting).task_status == "WAITING_HUMAN"


def test_recovery_refuses_an_unmigrated_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "unmigrated.db")
    try:
        with pytest.raises(RecoveryError, match="compatibility"):
            RecoveryService(database, slack_settings=SlackSettings()).recover()
    finally:
        database.dispose()


def test_doctor_detects_application_status_conflict(database: Database) -> None:
    application_id, _ = application(database)
    with database.transaction() as session:
        details = session.get(ApplicationDetails, application_id)
        assert details
        details.application_status = "FORM_READY"
    result = Doctor(database).run(RuntimeMode.DRY_RUN)
    checks = result["checks"]
    assert isinstance(checks, list)
    check = next(
        item
        for item in checks
        if isinstance(item, dict) and item["name"] == "application_status_consistency"
    )
    assert check["level"] == "FAIL"
    assert result["architecture_ready"] is False
    assert result["safe_to_enable_live"] is False


def test_missing_source_resume_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source_resume_template_missing"):
        ResumePolicy(resume_template=tmp_path / "missing.tex").validate_paths()


def test_evaluation_datasets_are_versioned_and_green() -> None:
    result = evaluate_all()
    assert result["datasets"] == 4
    assert result["cases"] == 13
    assert result["failed"] == 0


def test_reports_are_read_only_and_include_operational_risk(database: Database) -> None:
    application_id, task_id = application(database)
    reports = OperationalReports(database, clock=CLOCK)
    assert reports.queue()["total"] == 1
    applications = reports.applications()
    rows = applications["applications"]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    assert rows[0]["application_id"] == application_id
    assert reports.costs()["estimated_total"] == 0
    assert reports.status("DRY_RUN")["live_enabled"] is False
    with database.transaction() as session:
        assert session.get(AgentTask, task_id) is not None


@pytest.mark.parametrize(
    "command",
    ["status", "queue", "applications", "costs", "doctor"],
)
def test_required_read_only_commands(command: str, database: Database) -> None:
    result = runner.invoke(app, [command, "--database", str(database.path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)


def test_recover_and_integrity_commands(database: Database) -> None:
    recovered = runner.invoke(app, ["recover", "--database", str(database.path)])
    integrity = runner.invoke(app, ["db", "integrity", "--database", str(database.path)])
    assert recovered.exit_code == integrity.exit_code == 0
    assert json.loads(recovered.stdout)["total"] == 0
    assert json.loads(integrity.stdout)["ok"] is True
