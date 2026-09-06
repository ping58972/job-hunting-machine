"""Explicitly gated Phase 9 review and final-submission workers."""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer

from job_hunting_machine.browser.config import browser_settings
from job_hunting_machine.browser.manager import BrowserManager
from job_hunting_machine.config import load_settings
from job_hunting_machine.database.engine import DATABASE_PATH, Database
from job_hunting_machine.orchestration.queue import QueueService
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.slack.config import load_slack_settings
from job_hunting_machine.slack.control import SlackControlPlane
from job_hunting_machine.submission.playwright_adapter import PlaywrightSubmissionAdapter
from job_hunting_machine.submission.review import ReviewWorker
from job_hunting_machine.submission.worker import SubmissionWorker

app = typer.Typer(help="Phase 9 immutable review and explicitly approved submission.")


@app.command("review-worker")
def review_worker(
    once: Annotated[bool, typer.Option(help="Run at most one CREATE_REVIEW task.")] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Generate immutable review payloads; this command cannot submit."""
    database = Database(database_path)
    queue = QueueService(database)
    control = SlackControlPlane(database, load_slack_settings())
    runner = ReviewWorker(queue, slack=control)

    async def run() -> None:
        await runner.startup()
        if once:
            typer.echo(json.dumps({"phase": 9, "executed": await runner.run_once()}))
        else:
            await runner.run(install_signals=True)

    try:
        asyncio.run(run())
    finally:
        database.dispose()


@app.command("worker")
def submission_worker(
    once: Annotated[bool, typer.Option(help="Run at most one SUBMIT_APPLICATION task.")] = False,
    live: Annotated[
        bool, typer.Option(help="Enable the separately gated final-click agent.")
    ] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Report capability by default, or run the LIVE-only Submission Agent."""
    configured = load_settings().runtime_mode
    if not live:
        typer.echo(
            json.dumps(
                {
                    "phase": 9,
                    "runtime_mode": configured.value,
                    "submission_enabled": False,
                }
            )
        )
        return
    if configured is not RuntimeMode.LIVE or os.environ.get("SUBMISSION_ALLOW_LIVE") != "1":
        typer.echo("Submission LIVE opt-in requirements are not satisfied.", err=True)
        raise typer.Exit(code=2)
    settings = browser_settings(RuntimeMode.LIVE, live_flag=True)
    slack = load_slack_settings()
    database = Database(database_path)
    manager = BrowserManager(settings)
    runner = SubmissionWorker(
        QueueService(database),
        PlaywrightSubmissionAdapter(manager),
        runtime_mode=RuntimeMode.LIVE,
        authorized_user_ids=slack.authorized_user_ids,
    )

    async def run() -> None:
        try:
            await runner.startup()
            if once:
                typer.echo(json.dumps({"phase": 9, "executed": await runner.run_once()}))
            else:
                await runner.run(install_signals=True)
        finally:
            await manager.close()

    try:
        asyncio.run(run())
    finally:
        database.dispose()
