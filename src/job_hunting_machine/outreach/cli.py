"""Offline-by-default Phase 10 Connector and Outreach Sender commands."""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer

from job_hunting_machine.config import load_settings
from job_hunting_machine.database.engine import DATABASE_PATH, Database
from job_hunting_machine.orchestration.queue import QueueService
from job_hunting_machine.outreach.actions import (
    ConnectorExternalActionService,
    ExternalActionService,
)
from job_hunting_machine.outreach.connector import ConnectorWorker
from job_hunting_machine.outreach.discovery import (
    ContactDiscovery,
    FakeContactReader,
    HTTPContactReader,
)
from job_hunting_machine.outreach.gmail import FakeGmailAdapter, GmailRestAdapter
from job_hunting_machine.outreach.manual import ManualOutreachWorker
from job_hunting_machine.outreach.sender import OutreachSender
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.slack.config import load_slack_settings
from job_hunting_machine.slack.control import SlackControlPlane

app = typer.Typer(help="Phase 10 public contact discovery and approval-bound outreach.")


@app.command("manual-review")
def manual_review(
    once: Annotated[
        bool, typer.Option(help="Run at most one approved LinkedIn-manual review task.")
    ] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Record a Slack decision for manual use; never open or operate LinkedIn."""
    database = Database(database_path)
    worker = ManualOutreachWorker(QueueService(database))

    async def run() -> None:
        await worker.startup()
        if once:
            typer.echo(json.dumps({"phase": 10, "executed": await worker.run_once()}))
        else:
            await worker.run(install_signals=True)

    try:
        asyncio.run(run())
    finally:
        database.dispose()


@app.command("connector")
def connector(
    once: Annotated[bool, typer.Option(help="Run at most one CONNECT_CONTACTS task.")] = False,
    staging: Annotated[bool, typer.Option(help="Use network-free fake adapters.")] = False,
    live: Annotated[bool, typer.Option(help="Use explicitly enabled public/Gmail APIs.")] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Report offline capability or run contact discovery and draft generation."""
    configured = load_settings().runtime_mode
    if not staging and not live:
        typer.echo(
            json.dumps(
                {
                    "phase": 10,
                    "runtime_mode": configured.value,
                    "contact_discovery": False,
                    "gmail_write": False,
                    "linkedin_automation": False,
                }
            )
        )
        return
    if staging == live:
        typer.echo("Choose exactly one of --staging or --live.", err=True)
        raise typer.Exit(code=2)
    if live and configured is not RuntimeMode.LIVE:
        typer.echo("Connector LIVE mode is not configured.", err=True)
        raise typer.Exit(code=2)
    mode = RuntimeMode.STAGING if staging else RuntimeMode.LIVE
    reader = FakeContactReader() if staging else HTTPContactReader()
    gmail = FakeGmailAdapter() if staging else GmailRestAdapter()
    database = Database(database_path)
    queue = QueueService(database)
    slack_settings = load_slack_settings()
    actions = ExternalActionService(
        queue,
        gmail,
        runtime_mode=mode,
        authorized_user_ids=slack_settings.authorized_user_ids,
    )
    worker = ConnectorWorker(
        queue,
        ContactDiscovery(reader),
        ConnectorExternalActionService(actions),
        slack=SlackControlPlane(database, slack_settings),
    )

    async def run() -> None:
        await worker.startup()
        if once:
            typer.echo(json.dumps({"phase": 10, "executed": await worker.run_once()}))
        else:
            await worker.run(install_signals=True)

    try:
        asyncio.run(run())
    finally:
        database.dispose()


@app.command("sender")
def sender(
    once: Annotated[bool, typer.Option(help="Run at most one SEND_OUTREACH_EMAIL task.")] = False,
    live: Annotated[bool, typer.Option(help="Enable the separately gated email sender.")] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Report disabled capability by default, or run the LIVE-only Outreach Sender."""
    configured = load_settings().runtime_mode
    if not live:
        typer.echo(json.dumps({"phase": 10, "runtime_mode": configured.value, "email_send": False}))
        return
    if configured is not RuntimeMode.LIVE or os.environ.get("OUTREACH_SEND_ALLOW_LIVE") != "1":
        typer.echo("Outreach Sender LIVE opt-in requirements are not satisfied.", err=True)
        raise typer.Exit(code=2)
    settings = load_slack_settings()
    database = Database(database_path)
    queue = QueueService(database)
    actions = ExternalActionService(
        queue,
        GmailRestAdapter(),
        runtime_mode=RuntimeMode.LIVE,
        authorized_user_ids=settings.authorized_user_ids,
    )
    worker = OutreachSender(queue, actions)

    async def run() -> None:
        await worker.startup()
        if once:
            typer.echo(json.dumps({"phase": 10, "executed": await worker.run_once()}))
        else:
            await worker.run(install_signals=True)

    try:
        asyncio.run(run())
    finally:
        database.dispose()
