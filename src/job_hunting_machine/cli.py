"""Read-only foundation CLI. There is intentionally no workflow execution command."""

import json
from pathlib import Path
from typing import Annotated

import typer

from job_hunting_machine import __version__
from job_hunting_machine.config import ConfigurationError, load_settings
from job_hunting_machine.observability.logging import configure_logging
from job_hunting_machine.security.paths import PROJECT_ROOT

app = typer.Typer(
    name="jhm",
    help="Job Hunting Machine: Phase 0 foundation. No workflows or external operations.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


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
                "phase": 0,
                "workflow_available": False,
            },
            indent=2,
        )
    )
