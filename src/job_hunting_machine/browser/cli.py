"""Safe Phase 8 CLI: offline by default, fake ATS in explicit staging."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from job_hunting_machine.browser.config import browser_settings
from job_hunting_machine.browser.fake import FAKE_ATS_ORIGIN, FakeATSApplication
from job_hunting_machine.browser.manager import BrowserManager
from job_hunting_machine.browser.worker import FormWorker
from job_hunting_machine.config import load_settings
from job_hunting_machine.database.engine import DATABASE_PATH, Database
from job_hunting_machine.orchestration.queue import QueueService
from job_hunting_machine.runtime import RuntimeMode

app = typer.Typer(help="Phase 8 form preparation. Final submission is unavailable.")


@app.command("worker")
def worker(
    once: Annotated[bool, typer.Option(help="Run at most one FORM_PROCESS task.")] = False,
    staging: Annotated[bool, typer.Option(help="Use the network-free local fake ATS.")] = False,
    live: Annotated[bool, typer.Option(help="Prepare a real site with all LIVE gates.")] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Inspect capability or run the durable form worker through review only."""
    configured = load_settings().runtime_mode
    if not staging and not live:
        typer.echo(
            json.dumps(
                {
                    "phase": 8,
                    "runtime_mode": configured.value,
                    "browser_mutation": False,
                    "final_submission_available": False,
                }
            )
        )
        return
    if staging and live:
        typer.echo("Choose either --staging or --live.", err=True)
        raise typer.Exit(code=2)
    mode = RuntimeMode.STAGING if staging else configured
    try:
        settings = browser_settings(mode, live_flag=live)
    except ValueError:
        typer.echo("Form browser opt-in requirements are not satisfied.", err=True)
        raise typer.Exit(code=2) from None
    database = Database(database_path)
    fake = FakeATSApplication() if staging else None
    manager = BrowserManager(settings, fake=fake)
    runner = FormWorker(QueueService(database), manager)

    async def run() -> None:
        try:
            await runner.startup()
            if once:
                executed = await runner.run_once()
                typer.echo(
                    json.dumps(
                        {
                            "phase": 8,
                            "runtime_mode": mode.value,
                            "executed": executed,
                            "fake_origin": FAKE_ATS_ORIGIN if staging else None,
                            "final_submission_available": False,
                        }
                    )
                )
            else:
                await runner.run(install_signals=True)
        finally:
            await runner.close()

    try:
        asyncio.run(run())
    except (OSError, ValueError, RuntimeError):
        typer.echo("Form worker stopped safely; inspect local task and audit state.", err=True)
        raise typer.Exit(code=2) from None
    finally:
        database.dispose()
