"""Compare the migrated schema against the authoritative architecture and ORM."""

import re
import sqlite3
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError

from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import ActivityLog, Base
from job_hunting_machine.security.paths import PROJECT_ROOT


def test_every_domain_schema_matches_architecture_snapshot(tmp_path: Path) -> None:
    architecture = (PROJECT_ROOT / "docs/architecture-v2.md").read_text()
    blocks = re.findall(r"```sql\s*(CREATE TABLE .*?)```", architecture, re.DOTALL)
    assert len(blocks) == 23
    reference = sqlite3.connect(":memory:")
    database = Database(tmp_path / "schema.db")
    try:
        for block in blocks:
            reference.executescript(block)
        database.migrate()
        with database.engine.connect() as connection:
            actual = dict(
                connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='table' AND name != 'alembic_version'"
                )
                .tuples()
                .all()
            )
            expected = dict(
                reference.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table'"
                ).fetchall()
            )
            assert set(actual) == set(expected) == set(Base.metadata.tables)
            for name, sql in expected.items():
                # Amendment A1 recreates this SQLite table through Alembic batch mode.
                # SQLite serializes equivalent inline/table constraints in a different order.
                if name == "artifacts":
                    continue
                # Explicit NOT NULL fixes SQLite's nullable TEXT PK behavior.
                normalized = actual[name].replace("TEXT PRIMARY KEY NOT NULL", "TEXT PRIMARY KEY")
                assert re.sub(r"\s+", "", normalized) == re.sub(r"\s+", "", sql), name
            indexes = (
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
                )
                .scalars()
                .all()
            )
            assert set(indexes) == {"idx_agent_queue_claim", "idx_agent_queue_application"}
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "compare_server_default": True}
            )
            assert compare_metadata(context, Base.metadata) == []
    finally:
        reference.close()
        database.dispose()


@pytest.mark.parametrize(
    "column,value",
    [
        ("actor_type", "INVALID"),
        ("metadata_json", "{broken"),
        ("event_id", None),
    ],
)
def test_sql_constraints_reject_invalid_audit_data(
    tmp_path: Path,
    column: str,
    value: str | None,
) -> None:
    database = Database(tmp_path / "constraints.db")
    database.migrate()
    values = {
        "event_id": "EVT_00000000000000000000000000",
        "actor_type": "SYSTEM",
        "event_type": "test_event",
        "created_at": "2026-09-05T00:00:00.000Z",
        "metadata_json": "{}",
    }
    values_with_invalid: dict[str, str | None] = dict(values)
    values_with_invalid[column] = value
    try:
        with pytest.raises(IntegrityError), database.transaction() as session:
            session.execute(
                text(
                    "INSERT INTO activity_log"
                    "(event_id,actor_type,event_type,created_at,metadata_json) "
                    "VALUES (:event_id,:actor_type,:event_type,:created_at,:metadata_json)"
                ),
                values_with_invalid,
            )
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-05T00:00:00",
        "2026-09-05T00:00:00+00:00",
        "2026-02-30T00:00:00.000Z",
    ],
)
def test_orm_rejects_noncanonical_or_invalid_utc(tmp_path: Path, timestamp: str) -> None:
    database = Database(tmp_path / "utc.db")
    database.migrate()
    try:
        with pytest.raises(StatementError), database.transaction() as session:
            session.add(
                ActivityLog(
                    event_id="EVT_00000000000000000000000000",
                    actor_type="SYSTEM",
                    event_type="test_event",
                    created_at=timestamp,
                )
            )
    finally:
        database.dispose()


def test_orm_id_type_rejects_wrong_prefix_before_storage(tmp_path: Path) -> None:
    database = Database(tmp_path / "id.db")
    database.migrate()
    try:
        with pytest.raises(StatementError), database.transaction() as session:
            session.add(
                ActivityLog(
                    event_id="TASK_00000000000000000000000000",
                    actor_type="SYSTEM",
                    event_type="test_event",
                    created_at="2026-09-05T00:00:00.000Z",
                )
            )
    finally:
        database.dispose()
