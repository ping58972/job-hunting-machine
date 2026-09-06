"""Independent SQLite persistence, transaction, and filesystem-boundary acceptance."""

import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from job_hunting_machine.database.engine import Database, create_engine
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuardError

_EVENT_ID = "EVT_01K4Y9DKQF0S1NHV0A84NJBYPM"
_TIMESTAMP = "2026-09-05T08:23:31.123Z"
_DOMAIN_TABLES = {
    "qualification_rule_sets",
    "qualification_rules",
    "salary_location_rules",
    "check_job_position_quality",
    "application_pipeline",
    "application_details",
    "agent_queue",
    "task_memory",
    "activity_log",
    "slack_events",
    "my_information_for_filling_form",
    "candidate_facts",
    "project_catalog",
    "skill_catalog",
    "artifacts",
    "form_answers",
    "browser_sessions",
    "approvals",
    "external_actions",
    "contacts",
    "outreach_drafts",
    "monitor_events",
    "model_usage",
}
_INSERT_EVENT = text(
    "INSERT INTO activity_log (event_id, actor_type, event_type, created_at) "
    "VALUES (:event_id, 'SYSTEM', 'engine_acceptance', :created_at)"
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    instance = Database(tmp_path / "job-hunting.db")
    instance.migrate()
    try:
        yield instance
    finally:
        instance.dispose()


def test_migration_from_empty_database_creates_all_architecture_tables(tmp_path: Path) -> None:
    path = tmp_path / "job-hunting.db"
    assert not path.exists()
    database = Database(path)
    try:
        database.migrate()
        assert set(inspect(database.engine).get_table_names()) == _DOMAIN_TABLES | {
            "alembic_version"
        }
        with database.engine.connect() as connection:
            revisions = connection.execute(text("SELECT version_num FROM alembic_version")).all()
            assert len(revisions) == 1
            assert revisions[0][0]
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    finally:
        database.dispose()


def test_migration_is_repeatable_without_removing_existing_data(database: Database) -> None:
    with database.transaction() as session:
        session.execute(_INSERT_EVENT, {"event_id": _EVENT_ID, "created_at": _TIMESTAMP})
    database.migrate()
    with database.transaction() as session:
        assert session.execute(text("SELECT event_id FROM activity_log")).scalars().all() == [
            _EVENT_ID
        ]


def test_latex_artifact_migration_preserves_legacy_docx_rows(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy.db")
    configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
    configuration.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "src/job_hunting_machine/database/migrations"),
    )
    try:
        with database.engine.begin() as connection:
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, "0001_architecture_v2")
            connection.execute(
                text(
                    "INSERT INTO artifacts "
                    "(artifact_id, artifact_type, path, sha256, mime_type, created_at) VALUES "
                    "('ART_01K4Y9DKQF0S1NHV0A84NJBYPM', 'RESUME_DOCX', 'legacy.docx', "
                    ":sha, "
                    "'application/vnd.openxmlformats-officedocument.wordprocessingml.document', "
                    ":stamp)"
                ),
                {"sha": "a" * 64, "stamp": _TIMESTAMP},
            )
        database.migrate()
        with database.transaction() as session:
            assert session.execute(text("SELECT artifact_type FROM artifacts")).scalar_one() == (
                "RESUME_DOCX"
            )
            session.execute(
                text(
                    "INSERT INTO artifacts "
                    "(artifact_id, artifact_type, path, sha256, mime_type, created_at) VALUES "
                    "('ART_01K4Y9DKQF0S1NHV0A84NJBYPN', 'RESUME_TEX', 'resume.tex', "
                    ":sha, 'application/x-tex', :stamp)"
                ),
                {"sha": "b" * 64, "stamp": _TIMESTAMP},
            )
    finally:
        database.dispose()


def test_every_physical_connection_has_architecture_pragmas(database: Database) -> None:
    with database.engine.connect() as first, database.engine.connect() as second:
        assert first.connection.driver_connection is not second.connection.driver_connection
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000


def test_foreign_keys_reject_orphaned_task_memory(database: Database) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), database.transaction() as session:
        session.execute(
            text(
                "INSERT INTO task_memory (task_id, state_json, updated_at) "
                "VALUES (:task_id, '{}', :updated_at)"
            ),
            {"task_id": "TASK_01K4Y9DKQF0S1NHV0A84NJBYPM", "updated_at": _TIMESTAMP},
        )
    with database.transaction() as session:
        assert session.execute(text("SELECT COUNT(*) FROM task_memory")).scalar_one() == 0


def test_transaction_rolls_back_flushed_dml(database: Database) -> None:
    with (
        pytest.raises(RuntimeError, match="injected_after_write"),
        database.transaction() as session,
    ):
        session.execute(_INSERT_EVENT, {"event_id": _EVENT_ID, "created_at": _TIMESTAMP})
        session.flush()
        assert session.execute(text("SELECT COUNT(*) FROM activity_log")).scalar_one() == 1
        raise RuntimeError("injected_after_write")
    with database.transaction() as session:
        assert session.execute(text("SELECT COUNT(*) FROM activity_log")).scalar_one() == 0


def test_explicit_transaction_rolls_back_ddl_in_isolated_test_database(database: Database) -> None:
    # A synthetic table only in this test DB exposes sqlite3 legacy DDL autocommit.
    # Production domain schema is always created by the Alembic migration.
    with pytest.raises(RuntimeError, match="injected_after_ddl"), database.transaction() as session:
        session.execute(text("CREATE TABLE rollback_probe (probe_id INTEGER PRIMARY KEY)"))
        session.execute(text("INSERT INTO rollback_probe (probe_id) VALUES (1)"))
        raise RuntimeError("injected_after_ddl")
    assert "rollback_probe" not in inspect(database.engine).get_table_names()


def test_restart_preserves_committed_event_id_and_utc_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "job-hunting.db"
    first = Database(path)
    try:
        first.migrate()
        with first.transaction() as session:
            session.execute(_INSERT_EVENT, {"event_id": _EVENT_ID, "created_at": _TIMESTAMP})
    finally:
        first.dispose()
    restarted = Database(path)
    try:
        with restarted.transaction() as session:
            row = session.execute(text("SELECT event_id, created_at FROM activity_log")).one()
            assert tuple(row) == (_EVENT_ID, _TIMESTAMP)
    finally:
        restarted.dispose()


def test_new_database_and_sqlite_sidecars_are_private(tmp_path: Path) -> None:
    path = tmp_path / "private" / "job-hunting.db"
    database = Database(path)
    try:
        database.migrate()
        with database.engine.connect() as connection:
            connection.exec_driver_sql("SELECT COUNT(*) FROM activity_log").scalar_one()
            for candidate in [path, Path(f"{path}-wal"), Path(f"{path}-shm")]:
                assert candidate.exists()
                assert stat.S_IMODE(candidate.stat().st_mode) & 0o077 == 0
            assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "path",
    [PROJECT_ROOT.parent / "jhm-forbidden.db", Path("../jhm-forbidden.db")],
)
def test_database_rejects_paths_outside_fixed_root(path: Path) -> None:
    with pytest.raises(PathGuardError):
        engine = create_engine(path)
        try:
            with engine.connect():
                pass
        finally:
            engine.dispose()


def test_application_engine_rejects_langgraph_checkpoint_database(tmp_path: Path) -> None:
    path = tmp_path / "langgraph-checkpoints.db"
    with pytest.raises(ValueError):
        engine = create_engine(path)
        try:
            with engine.connect():
                pass
        finally:
            engine.dispose()
    assert not path.exists()


def test_database_symlink_does_not_modify_target(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"unchanged")
    path = tmp_path / "job-hunting.db"
    path.symlink_to(sentinel)
    with pytest.raises(PathGuardError):
        engine = create_engine(path)
        try:
            with engine.connect():
                pass
        finally:
            engine.dispose()
    assert sentinel.read_bytes() == b"unchanged"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_database_rejects_existing_symlink_sidecars(tmp_path: Path, suffix: str) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"unchanged")
    path = tmp_path / "job-hunting.db"
    Path(f"{path}{suffix}").symlink_to(sentinel)
    with pytest.raises(PathGuardError):
        engine = create_engine(path)
        try:
            with engine.connect():
                pass
        finally:
            engine.dispose()
    assert sentinel.read_bytes() == b"unchanged"


def test_statement_rechecks_sidecar_paths(database: Database, tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"unchanged")
    journal = tmp_path / "job-hunting.db-journal"
    with database.engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")
        journal.symlink_to(sentinel)
        try:
            with pytest.raises(PathGuardError):
                connection.exec_driver_sql("SELECT 1")
        finally:
            journal.unlink()
    assert sentinel.read_bytes() == b"unchanged"


@pytest.mark.parametrize("operation", ["ATTACH DATABASE ? AS escaped", "VACUUM INTO ?"])
def test_sql_cannot_redirect_writes_to_another_database(
    database: Database, tmp_path: Path, operation: str
) -> None:
    # The attack target remains local even if the implementation regresses.
    target = tmp_path / "forbidden-extra.db"
    with database.engine.connect() as connection:
        native = connection.connection.driver_connection
        assert isinstance(native, sqlite3.Connection)
        # Native execution specifically tests the authorizer. VACUUM through a
        # SQLAlchemy transaction could fail for unrelated transaction semantics.
        with pytest.raises(sqlite3.DatabaseError, match=r"not authorized|authorization denied"):
            native.execute(operation, (str(target),))
    assert not target.exists()


@pytest.mark.parametrize(
    "statement",
    [
        "PRAGMA foreign_keys = OFF",
        "PRAGMA journal_mode = DELETE",
        "PRAGMA synchronous = OFF",
        "PRAGMA busy_timeout = 1",
        "PRAGMA temp_store = FILE",
        "PRAGMA writable_schema = ON",
    ],
)
def test_native_sql_cannot_disable_safety_pragmas(database: Database, statement: str) -> None:
    with database.engine.connect() as connection:
        native = connection.connection.driver_connection
        assert isinstance(native, sqlite3.Connection)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            native.execute(statement)
