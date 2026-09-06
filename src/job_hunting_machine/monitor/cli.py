"""Offline-by-default Phase 11 scheduler and monitor worker commands."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from job_hunting_machine.config import load_settings
from job_hunting_machine.database.engine import DATABASE_PATH, Database
from job_hunting_machine.models.gateway import ModelGateway
from job_hunting_machine.monitor.classification import HybridStatusClassifier, LunaStatusClassifier
from job_hunting_machine.monitor.config import load_monitor_settings
from job_hunting_machine.monitor.gmail import FakeGmailReader, GmailRestReader
from job_hunting_machine.monitor.portal import FakePortalReader, HTTPPortalReader
from job_hunting_machine.monitor.scheduler import MonitorScheduler
from job_hunting_machine.monitor.service import MonitorService
from job_hunting_machine.monitor.types import GmailReader, PortalReader
from job_hunting_machine.monitor.worker import MonitorWorker
from job_hunting_machine.orchestration.queue import QueueService, RetryPolicy
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.slack.config import load_slack_settings
from job_hunting_machine.slack.control import SlackControlPlane

app = typer.Typer(help="Phase 11 read-only application monitoring.")


@app.command("schedule")
def schedule(
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Enqueue due active applications without contacting any external service."""
    database = Database(database_path)
    try:
        task_ids = MonitorScheduler(database, load_monitor_settings()).schedule_due()
        typer.echo(json.dumps({"phase": 11, "scheduled": len(task_ids), "task_ids": task_ids}))
    finally:
        database.dispose()


@app.command("worker")
def worker(
    once: Annotated[bool, typer.Option(help="Run at most one MONITOR_APPLICATION task.")] = False,
    staging: Annotated[bool, typer.Option(help="Use network-free fake readers.")] = False,
    live: Annotated[
        bool, typer.Option(help="Use explicitly enabled read-only transports.")
    ] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Report capability by default; external reads require all explicit live gates."""
    configured = load_settings().runtime_mode
    if not staging and not live:
        typer.echo(
            json.dumps(
                {
                    "phase": 11,
                    "runtime_mode": configured.value,
                    "gmail_read": False,
                    "portal_read": False,
                    "external_mutation": False,
                }
            )
        )
        return
    if staging == live:
        typer.echo("Choose exactly one of --staging or --live.", err=True)
        raise typer.Exit(code=2)
    if live and configured is not RuntimeMode.LIVE:
        typer.echo("Live monitoring requires configured LIVE mode.", err=True)
        raise typer.Exit(code=2)
    if staging and configured is RuntimeMode.LIVE:
        typer.echo("Staging readers cannot run under configured LIVE mode.", err=True)
        raise typer.Exit(code=2)

    database = Database(database_path)
    settings = load_monitor_settings()
    gateway: ModelGateway | None = None
    try:
        if live:
            gmail: GmailReader = GmailRestReader()
            portal: PortalReader = HTTPPortalReader()
            gateway = ModelGateway.for_openai(database)
            classifier = HybridStatusClassifier(LunaStatusClassifier(gateway))
        else:
            gmail = FakeGmailReader()
            portal = FakePortalReader()
            classifier = HybridStatusClassifier()
        queue = QueueService(
            database,
            retry_policy=RetryPolicy(delays=settings.retry_delays_seconds),
        )
        slack_settings = load_slack_settings()
        slack = (
            SlackControlPlane(database, slack_settings)
            if slack_settings.notification_channel_id
            else None
        )
        monitor = MonitorWorker(
            queue,
            MonitorService(database, classifier, settings),
            gmail,
            portal,
            settings,
            slack=slack,
        )

        async def run() -> None:
            recovered = await monitor.startup()
            executed = await monitor.run_once()
            typer.echo(
                json.dumps(
                    {
                        "phase": 11,
                        "runtime_mode": configured.value,
                        "recovered": recovered,
                        "executed": executed,
                    }
                )
            )
            if not once:
                while await monitor.run_once():
                    pass

        asyncio.run(run())
    finally:
        if gateway is not None:
            asyncio.run(gateway.close())
        database.dispose()
