"""Phase 7 administration. Normal invocation is offline capability inspection."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from job_hunting_machine.database.engine import DATABASE_PATH, Database
from job_hunting_machine.models.gateway import ModelGateway
from job_hunting_machine.orchestration import QueueService
from job_hunting_machine.resume.config import load_policy
from job_hunting_machine.resume.documents import GoogleDocsHTTP
from job_hunting_machine.resume.template import propose_template
from job_hunting_machine.resume.worker import ResumeWorker
from job_hunting_machine.security.paths import PROJECT_ROOT

app = typer.Typer(help="Native resume and cover-letter artifacts; no form processing.")


@app.command("template-propose")
def template_proposal(
    folder_id: Annotated[str, typer.Option()],
    live_docs: Annotated[bool, typer.Option()] = False,
    policy_path: Annotated[Path, typer.Option("--policy")] = PROJECT_ROOT / "config/resume.yaml",
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Capture template evidence as UNVERIFIED for explicit local human review."""
    if not live_docs:
        typer.echo("Template capture requires --live-docs and the Google Docs opt-in.", err=True)
        raise typer.Exit(code=2)
    policy = load_policy(policy_path)
    adapter = GoogleDocsHTTP(folder_id=folder_id)
    database = Database(database_path)
    try:
        typer.echo(propose_template(database, adapter, policy))
    finally:
        adapter.close()
        database.dispose()


@app.command("worker")
def document_worker(
    live_docs: Annotated[bool, typer.Option()] = False,
    live_models: Annotated[bool, typer.Option()] = False,
    folder_id: Annotated[str, typer.Option()] = "",
    once: Annotated[bool, typer.Option()] = False,
    policy_path: Annotated[Path, typer.Option("--policy")] = PROJECT_ROOT / "config/resume.yaml",
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Require both live flags, provider opt-ins, credentials and a destination folder.

    Programmatic offline tests inject FakeDocuments and a scripted ModelGateway.
    This command does not manufacture candidate evidence or model responses.
    """
    if not live_docs and not live_models:
        typer.echo('{"phase":7,"live_docs":false,"live_models":false,"form_processing":false}')
        return
    if not live_docs or not live_models or not folder_id:
        typer.echo(
            "Both live flags and --folder-id are required for document generation.", err=True
        )
        raise typer.Exit(code=2)
    policy = load_policy(policy_path)
    adapter = GoogleDocsHTTP(folder_id=folder_id)
    database = Database(database_path)

    async def run() -> None:
        gateway = ModelGateway.for_openai(database)
        try:
            worker = ResumeWorker(QueueService(database), adapter, gateway, policy=policy)
            if once:
                await worker.startup()
                typer.echo(str(await worker.run_once()).lower())
            else:
                await worker.run(install_signals=True)
        finally:
            await gateway.close()

    try:
        asyncio.run(run())
    finally:
        adapter.close()
        database.dispose()
