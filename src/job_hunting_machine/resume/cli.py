"""Local LaTeX document worker and environment inspection."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from job_hunting_machine.database.engine import DATABASE_PATH, Database
from job_hunting_machine.models.gateway import ModelGateway
from job_hunting_machine.orchestration import QueueService
from job_hunting_machine.resume.config import load_policy
from job_hunting_machine.resume.latex import (
    COVER_LETTER,
    PROJECTS,
    SKILLS,
    LatexCompiler,
    LatexTemplate,
)
from job_hunting_machine.resume.worker import ResumeWorker
from job_hunting_machine.security.paths import PROJECT_ROOT

app = typer.Typer(help="Local LaTeX resume and cover-letter generation.")


@app.command("check")
def check(
    policy_path: Annotated[Path, typer.Option("--policy")] = PROJECT_ROOT / "config/resume.yaml",
) -> None:
    """Validate local templates and report the selected compiler without generating files."""
    policy = load_policy(policy_path)
    LatexTemplate(policy.resume_template.read_text(), (PROJECTS, SKILLS))
    LatexTemplate(policy.cover_letter_template.read_text(), (COVER_LETTER,))
    compiler = LatexCompiler(
        preferred=policy.preferred_compiler,
        timeout_seconds=policy.timeout_seconds,
    ).detect()
    typer.echo(
        json.dumps(
            {
                "resume_template": str(policy.resume_template),
                "cover_letter_template": str(policy.cover_letter_template),
                "compiler": compiler[0] if compiler else None,
                "ready": compiler is not None,
                "cloud_documents": False,
            },
            indent=2,
        )
    )
    if compiler is None:
        raise typer.Exit(code=2)


@app.command("worker")
def document_worker(
    live_models: Annotated[bool, typer.Option()] = False,
    once: Annotated[bool, typer.Option()] = False,
    policy_path: Annotated[Path, typer.Option("--policy")] = PROJECT_ROOT / "config/resume.yaml",
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Run local generation; live model construction remains explicitly gated."""
    if not live_models:
        typer.echo(
            '{"documents":"local_latex","live_models":false,'
            '"google_docs":false,"external_effects":false}'
        )
        return
    policy = load_policy(policy_path)
    database = Database(database_path)

    async def run() -> None:
        gateway = ModelGateway.for_openai(database)
        try:
            worker = ResumeWorker(QueueService(database), gateway, policy=policy)
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
        database.dispose()
