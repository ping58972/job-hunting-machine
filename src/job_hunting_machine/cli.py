"""Local configuration/database administration; no workflow execution command."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

from job_hunting_machine import __version__
from job_hunting_machine.config import ConfigurationError, load_settings
from job_hunting_machine.database.engine import DATABASE_PATH, Database, DatabaseError
from job_hunting_machine.observability.logging import configure_logging
from job_hunting_machine.security.paths import PROJECT_ROOT

app = typer.Typer(
    name="jhm",
    help="Job Hunting Machine: Phase 1 database. No workflows or external operations.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
database_app = typer.Typer(help="Local Alembic migration and policy seed administration.")
app.add_typer(database_app, name="db")


@app.command()
def version() -> None:
    """Print the installed foundation version."""
    typer.echo(__version__)


@app.command("config")
def show_config(
    runtime_file: Annotated[Path, typer.Option(help="Root-local runtime YAML.")] = (
        PROJECT_ROOT / "config/runtime.yaml"
    ),
    logging_file: Annotated[Path, typer.Option(help="Root-local logging YAML.")] = (
        PROJECT_ROOT / "config/logging.yaml"
    ),
    env_file: Annotated[Path, typer.Option(help="Optional root-local dotenv file.")] = (
        PROJECT_ROOT / ".env"
    ),
) -> None:
    """Validate configuration and print JSON without starting a runtime."""
    try:
        settings = load_settings(
            runtime_file=runtime_file, logging_file=logging_file, env_file=env_file
        )
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    logger = configure_logging(level=settings.log_level)
    logger.info("configuration_validated", extra={"agent": "cli", "status": "ok"})
    typer.echo(
        json.dumps(
            {
                "project_root": str(settings.project_root),
                "configured_runtime_mode": settings.runtime_mode.value,
                "log_level": settings.log_level,
                "phase": 1,
                "workflow_available": False,
            },
            indent=2,
        )
    )


@database_app.command("init")
def initialize_database(
    database_path: Annotated[
        Path, typer.Option("--database", help="Database .db path inside the fixed project root.")
    ] = DATABASE_PATH,
) -> None:
    """Apply Alembic migrations and seed policy data; safe to repeat.

    This administrative operation creates no jobs, applications, or candidate facts.
    """
    from job_hunting_machine.database.seeds import seed_policies

    database: Database | None = None
    try:
        database = Database(database_path)
        database.migrate()
        with database.transaction() as session:
            result = seed_policies(session)
        typer.echo(json.dumps({"database": str(database.path), "seed": asdict(result)}, indent=2))
    except (DatabaseError, CommandError, SQLAlchemyError, OSError, ValueError):
        typer.echo(
            "Database initialization failed; check the local path and schema version.", err=True
        )
        raise typer.Exit(code=2) from None
    finally:
        if database is not None:
            database.dispose()
