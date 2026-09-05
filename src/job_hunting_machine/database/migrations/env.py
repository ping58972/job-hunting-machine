"""Run migrations through the shared guarded SQLite engine."""

from pathlib import Path

from alembic import context
from sqlalchemy.engine import Connection

from job_hunting_machine.database.engine import create_engine
from job_hunting_machine.database.models import Base


def run_with_connection(connection: Connection) -> None:
    """Use the supplied connection without closing or committing its owner."""
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        transactional_ddl=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations() -> None:
    """Support SQL review output and guarded online application/test databases."""
    if context.is_offline_mode():
        context.configure(
            url="sqlite://",
            target_metadata=Base.metadata,
            literal_binds=True,
            transactional_ddl=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connection = context.config.attributes.get("connection")
    if connection is not None:
        if not isinstance(connection, Connection):
            raise TypeError("Alembic connection must be a SQLAlchemy Connection")
        run_with_connection(connection)
        return

    arguments = context.get_x_argument(as_dictionary=True)
    if set(arguments) - {"db"}:
        raise ValueError("Only the root-contained '-x db=...' Alembic argument is supported")
    engine = create_engine(Path(arguments["db"])) if "db" in arguments else create_engine()
    try:
        with engine.begin() as owned_connection:
            run_with_connection(owned_connection)
    finally:
        engine.dispose()


run_migrations()
