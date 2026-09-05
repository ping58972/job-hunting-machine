"""Root-confined SQLite engine and explicit Alembic/transaction boundaries.

SQLite opens its database and journal files natively. We reserve the database via
PathGuard, validate it and every sidecar at connection/statement/transaction
boundaries, and prohibit SQL that could attach or export another database. As
with PathGuard, the project's directories must remain trusted: this is not a
custom SQLite VFS or an OS sandbox against hostile concurrent namespace changes.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, event, inspect
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard, PathGuardError

DATABASE_PATH = PROJECT_ROOT / "data/job-hunting.db"
_CHECKPOINT_NAME = "langgraph-checkpoints.db"
_PROTECTED_PRAGMAS = frozenset(
    {
        "foreign_keys",
        "journal_mode",
        "synchronous",
        "busy_timeout",
        "temp_store",
        "temp_store_directory",
        "data_store_directory",
        "writable_schema",
    }
)


class DatabaseError(RuntimeError):
    """The requested operation cannot satisfy the database contract."""


def _authorize_sql(
    action: int,
    first: str | None,
    second: str | None,
    database: str | None,
    trigger: str | None,
) -> int:
    # SQLite implements VACUUM INTO through an internal ATTACH operation.
    if action == sqlite3.SQLITE_ATTACH:
        return sqlite3.SQLITE_DENY
    if (
        action == sqlite3.SQLITE_PRAGMA
        and first is not None
        and first.lower() in _PROTECTED_PRAGMAS
        and second is not None
    ):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and second == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _database_path(path: Path) -> Path:
    target = PathGuard().validate_write(path)
    if target.name == _CHECKPOINT_NAME:
        raise PathGuardError("The LangGraph checkpoint database is reserved for LangGraph")
    if target.suffix != ".db":
        raise PathGuardError("Application database paths must use the .db suffix")
    return target


def _validate_database_files(target: Path) -> None:
    guard = PathGuard()
    for suffix in ("", "-wal", "-shm", "-journal"):
        checked = guard.validate_write(Path(f"{target}{suffix}"))
        if checked.exists() and not checked.is_file():
            raise PathGuardError("Database and journal destinations must be regular files")


def create_engine(path: Path = DATABASE_PATH) -> Engine:
    """Create a local SQLite engine; schema creation is exclusively Alembic's job.

    NullPool makes connection ownership explicit and releases file handles between
    transactions. Each new connection re-applies the architecture PRAGMAs.
    """
    target = _database_path(path)
    guard = PathGuard()
    _validate_database_files(target)
    guard.mkdir(target.parent, parents=True, exist_ok=True)
    guard.prepare_private_file(target)

    def connect() -> sqlite3.Connection:
        _validate_database_files(target)
        connection = sqlite3.connect(
            f"{target.as_uri()}?mode=rw",
            uri=True,
            isolation_level=None,
            timeout=5.0,
            check_same_thread=False,
        )
        try:
            # Applied outside a transaction: SQLite ignores foreign_keys changes
            # inside transactions and cannot switch journal mode there.
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or mode[0] != "wal":
                raise DatabaseError("SQLite WAL mode could not be enabled")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.set_authorizer(_authorize_sql)
            return connection
        except BaseException:
            connection.close()
            raise

    engine = sqlalchemy_create_engine(
        "sqlite+pysqlite://",
        creator=connect,
        poolclass=NullPool,
        hide_parameters=True,
    )

    @event.listens_for(engine, "begin")
    def begin(connection: Connection) -> None:
        # Explicit BEGIN includes SELECT, SAVEPOINT, and DDL in the transaction;
        # sqlite3's legacy transaction mode does not provide that guarantee.
        connection.exec_driver_sql("BEGIN")

    @event.listens_for(engine, "before_cursor_execute")
    def before_statement(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        _validate_database_files(target)

    @event.listens_for(engine, "commit")
    @event.listens_for(engine, "rollback")
    def before_transaction_end(connection: Connection) -> None:
        _validate_database_files(target)

    return engine


class Database:
    """Own a database engine and short-lived all-or-nothing session transactions."""

    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = _database_path(path)
        self.engine = create_engine(self.path)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Commit only on success; exceptions roll back the complete unit of work.

        Repositories accept this session and never commit themselves. A caller
        must let transaction errors escape this context instead of swallowing them.
        """
        with self._sessions.begin() as session:
            yield session

    def migrate(self) -> None:
        """Upgrade this database through Alembic in one explicit transaction."""
        configuration = Config(str(PROJECT_ROOT / "alembic.ini"))
        configuration.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if tables and "alembic_version" not in tables:
                raise DatabaseError("Refusing to migrate an unversioned, nonempty database")
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, "head")

    def dispose(self) -> None:
        """Release engine resources; committed records remain on disk."""
        self.engine.dispose()
