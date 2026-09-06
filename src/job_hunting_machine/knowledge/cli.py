"""Explicit catalog administration; credentials and live access are never implicit."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, cast

import typer
from sqlalchemy import select

from job_hunting_machine.database.engine import DATABASE_PATH, Database
from job_hunting_machine.database.models import CandidateFact
from job_hunting_machine.database.repositories import TaskCreate
from job_hunting_machine.database.repositories.knowledge import (
    CandidateFactRepository,
    Verification,
)
from job_hunting_machine.knowledge.github import repository_name
from job_hunting_machine.knowledge.retrieval import retrieve
from job_hunting_machine.orchestration import QueueService

app = typer.Typer(help="Phase 6 source-backed knowledge. No resume editing.", no_args_is_help=True)


@app.command("scan")
def enqueue_scan(
    repository: str, database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH
) -> None:
    """Enqueue an incremental repository scan without making a network call."""
    repository = repository_name(repository)
    database = Database(database_path)
    try:
        typer.echo(
            QueueService(database).enqueue(
                TaskCreate("SCAN_GITHUB", payload={"repository": repository})
            )
        )
    finally:
        database.dispose()


@app.command("inventory")
def enqueue_inventory(
    owner: str, database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH
) -> None:
    """Enqueue bounded public repository inventory for an explicitly selected owner."""
    repository_name(f"{owner}/placeholder")
    database = Database(database_path)
    try:
        typer.echo(
            QueueService(database).enqueue(TaskCreate("GITHUB_INVENTORY", payload={"owner": owner}))
        )
    finally:
        database.dispose()


@app.command("worker")
def catalog_worker(
    live: Annotated[bool, typer.Option()] = False,
    once: Annotated[bool, typer.Option()] = False,
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Connect only with --live and GITHUB_ALLOW_LIVE=1; otherwise inspect capability."""
    import asyncio

    from job_hunting_machine.knowledge.github import GitHubHTTP
    from job_hunting_machine.knowledge.scanner import CatalogWorker

    if not live:
        typer.echo('{"phase":6,"live_github":false,"resume_editing":false}')
        return
    try:
        client = GitHubHTTP()
    except ValueError:
        typer.echo("Live GitHub requires GITHUB_ALLOW_LIVE=1.", err=True)
        raise typer.Exit(code=2) from None
    database = Database(database_path)
    worker = CatalogWorker(QueueService(database), client)

    async def run() -> None:
        if once:
            await worker.startup()
            typer.echo(json.dumps({"executed": await worker.run_once()}))
        else:
            await worker.run(install_signals=True)

    try:
        asyncio.run(run())
    finally:
        database.dispose()


@app.command("facts")
def inspect_facts(
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Inspect up to 200 local proposals, exact evidence, and hashes for human review."""
    database = Database(database_path)
    try:
        with database.transaction() as session:
            facts = list(
                session.scalars(select(CandidateFact).order_by(CandidateFact.fact_id).limit(200))
            )
            typer.echo(
                json.dumps(
                    [
                        {
                            "fact_id": f.fact_id,
                            "status": f.verification_status,
                            "value_sha256": hashlib.sha256(f.value_json.encode()).hexdigest(),
                            "value": json.loads(f.value_json),
                        }
                        for f in facts
                    ],
                    indent=2,
                )
            )
    finally:
        database.dispose()


@app.command("decide")
def decide_fact(
    fact_id: str,
    status: str,
    value_sha256: Annotated[str, typer.Option()],
    reviewer: Annotated[str, typer.Option()],
    expected: Annotated[str, typer.Option()] = "UNVERIFIED",
    database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH,
) -> None:
    """Explicitly record a human review of the exact inspected fact; no auto-verification."""
    database = Database(database_path)
    try:
        with database.transaction(immediate=True) as session:
            repository = CandidateFactRepository(session)
            fact = repository.get(fact_id)
            if hashlib.sha256(fact.value_json.encode()).hexdigest() != value_sha256:
                raise ValueError("reviewed_fact_hash_changed")
            repository.decide(
                fact_id,
                cast(Verification, status),
                expected_status=cast(Verification, expected),
                reviewer=reviewer,
            )
            typer.echo(json.dumps({"fact_id": fact.fact_id, "status": fact.verification_status}))
    except (ValueError, LookupError):
        typer.echo(
            "Fact review failed; check the fact hash, source, and expected status.", err=True
        )
        raise typer.Exit(code=2) from None
    finally:
        database.dispose()


@app.command("retrieve")
def retrieve_facts(
    requirements: str, database_path: Annotated[Path, typer.Option("--database")] = DATABASE_PATH
) -> None:
    """Return a deterministic verified-only shortlist; no model or GitHub calls."""
    database = Database(database_path)
    try:
        typer.echo(retrieve(database, requirements).model_dump_json(indent=2))
    finally:
        database.dispose()
