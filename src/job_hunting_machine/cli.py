"""Local administration, deterministic fixtures, and model configuration inspection."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

from job_hunting_machine import __version__
from job_hunting_machine.browser.cli import app as form_app
from job_hunting_machine.config import ConfigurationError, load_settings
from job_hunting_machine.database.engine import DATABASE_PATH, Database, DatabaseError
from job_hunting_machine.knowledge.cli import app as catalog_app
from job_hunting_machine.observability.logging import configure_logging
from job_hunting_machine.resume.cli import app as resume_app
from job_hunting_machine.security.paths import PROJECT_ROOT

app = typer.Typer(
    name="jhm",
    help="Job Hunting Machine: Phase 8 form preparation. Final submission is unavailable.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
database_app = typer.Typer(help="Local Alembic migration and policy seed administration.")
app.add_typer(database_app, name="db")
app.add_typer(catalog_app, name="catalog")
app.add_typer(resume_app, name="resume")
app.add_typer(form_app, name="form")


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
                "phase": 8,
                "form_preparation_available": True,
                "resume_artifacts_available": True,
                "candidate_knowledge_available": True,
                "qualification_available": True,
                "slack_control_available": True,
                "model_gateway_available": True,
                "workflow_available": True,
                "external_workflows_available": False,
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


queue_app = typer.Typer(help="Local durable queue and deterministic fixtures only.")
app.add_typer(queue_app, name="queue")


@queue_app.command("demo")
def enqueue_demo(
    human: Annotated[bool, typer.Option(help="Pause the fixture for human input.")] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Explicitly enqueue one synthetic task; no job or application is created."""
    from job_hunting_machine.database.repositories import TaskCreate
    from job_hunting_machine.orchestration import QueueService

    database = Database(database_path)
    try:
        task_id = QueueService(database).enqueue(
            TaskCreate("FAKE_HUMAN" if human else "FAKE", payload={"text": "synthetic fixture"})
        )
        typer.echo(task_id)
    finally:
        database.dispose()


@queue_app.command("inspect")
def inspect_task(
    task_id: str,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Display task state and concise memory locally (may contain human replies)."""
    from job_hunting_machine.orchestration import QueueService

    database = Database(database_path)
    try:
        queue = QueueService(database)
        row = queue.get(task_id)
        typer.echo(
            json.dumps(
                {
                    "task_id": row.task_id,
                    "status": row.task_status,
                    "attempt_count": row.attempt_count,
                    "memory": queue.memory(task_id),
                },
                indent=2,
            )
        )
    finally:
        database.dispose()


@queue_app.command("resume")
def resume_task(
    task_id: str,
    interrupt_id: Annotated[str, typer.Option()],
    reply: Annotated[str, typer.Option(help="JSON value; do not put secrets in shell history.")],
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Durably record an explicit human reply and make its task eligible again."""
    from job_hunting_machine.orchestration import QueueService

    database = Database(database_path)
    try:
        QueueService(database).resume(task_id, interrupt_id, json.loads(reply))
        typer.echo("Human reply persisted.")
    finally:
        database.dispose()


@app.command("worker")
def run_worker(
    once: Annotated[
        bool, typer.Option(help="Recover and run at most one eligible fixture.")
    ] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Run only FAKE / FAKE_HUMAN workflows, always locally in DRY_RUN.

    The checkpoint database is langgraph-checkpoints.db beside the selected application database.
    SIGINT/SIGTERM stop claims and drain active checkpoint writes before releasing ownership.
    """
    import asyncio

    from job_hunting_machine.orchestration import QueueService, Worker, fake_workflow

    database = Database(database_path)
    worker = Worker(
        QueueService(database),
        {
            "FAKE": fake_workflow(),
            "FAKE_HUMAN": fake_workflow(human=True),
        },
        checkpoint_path=database.path.parent / "langgraph-checkpoints.db",
    )

    async def run() -> None:
        if once:
            recovered = await worker.startup()
            executed = await worker.run_once()
            typer.echo(
                json.dumps(
                    {
                        "runtime_mode": worker.runtime_mode.value,
                        "recovered": recovered,
                        "executed": executed,
                    }
                )
            )
        else:
            await worker.run(install_signals=True)

    try:
        asyncio.run(run())
    finally:
        database.dispose()


@app.command("models")
def inspect_models() -> None:
    """Validate and show model routing/budget configuration without creating an API client."""
    from job_hunting_machine.models.router import load_registry

    try:
        registry = load_registry()
    except ConfigurationError:
        typer.echo("Model registry validation failed.", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(registry.model_dump_json(indent=2))


@app.command("slack")
def slack_control(
    live: Annotated[bool, typer.Option(help="Explicitly connect to Slack Socket Mode.")] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Inspect Slack settings; --live additionally requires SLACK_ALLOW_LIVE=1."""
    from job_hunting_machine.slack.config import SlackInputError, load_slack_settings
    from job_hunting_machine.slack.socket_mode import run_live

    database: Database | None = None
    try:
        settings = load_slack_settings()
        if not live:
            typer.echo(settings.model_dump_json(indent=2))
            return
        database = Database(database_path)
        run_live(database, settings=settings)
    except (ConfigurationError, SlackInputError, DatabaseError, SQLAlchemyError):
        typer.echo(
            "Slack control failed; check local configuration and database initialization.", err=True
        )
        raise typer.Exit(code=2) from None
    finally:
        if database is not None:
            database.dispose()


@app.command("qualify")
def run_qualification(
    once: Annotated[
        bool, typer.Option(help="Process at most one intake or qualification task.")
    ] = False,
    fetch_live: Annotated[bool, typer.Option(help="Opt into public HTTP job-page reads.")] = False,
    browser_live: Annotated[
        bool, typer.Option(help="Opt into read-only Playwright fallback.")
    ] = False,
    models_live: Annotated[
        bool, typer.Option(help="Opt into budgeted semantic ModelGateway calls.")
    ] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Run Phase 5 only. Live transports each additionally require their environment flag."""
    import asyncio

    from job_hunting_machine.agents.fetch import HTTPReader, JobFetcher, PlaywrightReader
    from job_hunting_machine.agents.worker import QualificationWorker, run_workers
    from job_hunting_machine.models.gateway import ModelGateway
    from job_hunting_machine.orchestration import QueueService

    if browser_live and not fetch_live:
        typer.echo("Browser fallback also requires --fetch-live.", err=True)
        raise typer.Exit(code=2)
    database = Database(database_path)

    async def run() -> None:
        gateway = ModelGateway.for_openai(database) if models_live else None
        try:
            fetcher = JobFetcher(
                HTTPReader() if fetch_live else None, PlaywrightReader() if browser_live else None
            )
            worker = QualificationWorker(QueueService(database), fetcher=fetcher, gateway=gateway)
            if not fetch_live:
                # Offline CLI can intake URLs, but must not classify real jobs from empty fixtures.
                worker.workflows.pop("QUALIFY_JOB")
            if once:
                await worker.startup()
                executed = await worker.run_once()
                typer.echo(
                    json.dumps({"phase": 5, "executed": executed, "submission_available": False})
                )
            else:
                workers = [worker]
                for _ in range(7):
                    peer = QualificationWorker(
                        QueueService(database), fetcher=fetcher, gateway=gateway
                    )
                    if not fetch_live:
                        peer.workflows.pop("QUALIFY_JOB")
                    workers.append(peer)
                await run_workers(workers)
        finally:
            if gateway is not None:
                await gateway.close()

    try:
        asyncio.run(run())
    except (ValueError, DatabaseError, ConfigurationError):
        typer.echo("Qualification runner failed; check local configuration and opt-ins.", err=True)
        raise typer.Exit(code=2) from None
    finally:
        database.dispose()
