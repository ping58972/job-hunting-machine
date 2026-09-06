"""Phase 6: synthetic GitHub, real durable repositories, and verified-only retrieval."""

import asyncio
import base64
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    ActivityLog,
    CandidateFact,
    ProjectCatalog,
    SkillCatalog,
)
from job_hunting_machine.database.repositories import TaskCreate
from job_hunting_machine.database.repositories.knowledge import (
    CandidateFactRepository,
    CatalogRepository,
)
from job_hunting_machine.knowledge.evidence import Provenance, store_evidence
from job_hunting_machine.knowledge.github import FakeGitHub, GitHubHTTP, inventory
from job_hunting_machine.knowledge.retrieval import retrieve
from job_hunting_machine.knowledge.scanner import CatalogWorker, read_manifest
from job_hunting_machine.orchestration import QueueService

REPO = "fixture/robot"
COMMIT = "a" * 40
TREE = "b" * 40


def fixtures(
    files: dict[str, str] | None = None, *, commit: str = COMMIT, repo: str = REPO
) -> dict[str, Any]:
    files = (
        files
        if files is not None
        else {
            "README.md": (
                "# Synthetic Robot\nRobotics navigation with Python.\n"
                "Reports 95% accuracy on a synthetic benchmark.\n"
            ),
            "src/robot.py": "import torch\nimport numpy as np\n",
        }
    )
    result: dict[str, Any] = {
        f"/repos/{repo}": {
            "full_name": repo,
            "default_branch": "main",
            "description": "Synthetic fixture",
            "topics": ["robotics"],
        },
        f"/repos/{repo}/commits/main": {"sha": commit, "commit": {"tree": {"sha": TREE}}},
    }
    entries = []
    for path, text in files.items():
        raw = text.encode()
        sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        entries.append(
            {"path": path, "sha": sha, "size": len(raw), "type": "blob", "mode": "100644"}
        )
        result[f"/repos/{repo}/git/blobs/{sha}"] = {
            "sha": sha,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
        }
    result[f"/repos/{repo}/git/trees/{TREE}?recursive=1"] = {
        "sha": TREE,
        "truncated": False,
        "tree": entries,
    }
    return result


@pytest.fixture
def queue(tmp_path: Path) -> Iterator[QueueService]:
    database = Database(tmp_path / "jobs.db")
    database.migrate()
    yield QueueService(database, clock=FrozenClock(datetime(2026, 9, 5, tzinfo=UTC)))
    database.dispose()


def worker(queue: QueueService, client: FakeGitHub) -> CatalogWorker:
    return CatalogWorker(queue, client, evidence_root=queue.database.path.parent / "evidence")


def scan(queue: QueueService, client: FakeGitHub, repo: str = REPO) -> str:
    task = queue.enqueue(TaskCreate("SCAN_GITHUB", payload={"repository": repo}))
    asyncio.run(worker(queue, client).run_once())
    assert queue.get(task).task_status == "SUCCEEDED"
    return task


def verify_all(queue: QueueService) -> None:
    with queue.database.transaction(immediate=True) as session:
        repository = CandidateFactRepository(session, queue.clock)
        for fact in list(session.scalars(select(CandidateFact))):
            if fact.verification_status == "UNVERIFIED" and repository.current(fact):
                repository.decide(
                    fact.fact_id,
                    "VERIFIED",
                    expected_status="UNVERIFIED",
                    reviewer="fixture_reviewer",
                )


def test_scan_provenance_and_no_automatic_verification(queue: QueueService) -> None:
    scan(queue, FakeGitHub(fixtures()))
    with queue.database.transaction() as session:
        project = session.scalars(select(ProjectCatalog)).one()
        facts = list(session.scalars(select(CandidateFact)))
        assert project.source_commit_sha == COMMIT
        assert len(facts) == 5
        assert all(f.verification_status == "UNVERIFIED" for f in facts)
        for fact in facts:
            provenance = Provenance.model_validate(json.loads(fact.value_json)["provenance"])
            provenance.validate_content()
            assert provenance.commit_sha == COMMIT
            assert COMMIT in fact.source_reference
        assert "95%" in " ".join(f.value_json for f in facts)
        assert "99%" not in " ".join(f.value_json for f in facts)
        assert json.loads(project.verified_facts_json or "[]") == []
    assert retrieve(queue.database, "Python robotics PyTorch").projects == []


def test_verification_enables_facts_skills_and_project_retrieval(queue: QueueService) -> None:
    scan(queue, FakeGitHub(fixtures()))
    verify_all(queue)
    result = retrieve(queue.database, "Python robotics PyTorch")
    assert len(result.projects) == 1
    assert result.facts
    with queue.database.transaction() as session:
        assert {s.canonical_name for s in CatalogRepository(session).verified_skills()} == {
            "Python",
            "PyTorch",
            "NumPy",
        }
        assert (
            len(
                json.loads(
                    session.scalars(select(ProjectCatalog)).one().verified_facts_json or "[]"
                )
            )
            == 5
        )
        assert (
            len(
                list(
                    session.scalars(
                        select(ActivityLog).where(
                            ActivityLog.event_type == "candidate_fact_reviewed"
                        )
                    )
                )
            )
            == 5
        )


def test_unchanged_commit_skips_tree_and_blobs(queue: QueueService) -> None:
    client = FakeGitHub(fixtures())
    scan(queue, client)
    client.calls.clear()
    scan(queue, client)
    assert client.calls == [f"/repos/{REPO}", f"/repos/{REPO}/commits/main"]
    with queue.database.transaction() as session:
        assert len(list(session.scalars(select(CandidateFact)))) == 5
        assert len(list(session.scalars(select(ProjectCatalog)))) == 1


def test_new_commit_reuses_blob_and_invalidates_prior_verification(queue: QueueService) -> None:
    scan(queue, FakeGitHub(fixtures()))
    verify_all(queue)
    client = FakeGitHub(
        fixtures(
            {
                "README.md": "# Robot\nChanged navigation project.\n",
                "src/robot.py": "import torch\nimport numpy as np\n",
            },
            commit="c" * 40,
        )
    )
    scan(queue, client)
    assert sum("/git/blobs/" in path for path in client.calls) == 1
    assert not retrieve(queue.database, "Python robotics").projects
    with queue.database.transaction() as session:
        project = session.scalars(select(ProjectCatalog)).one()
        assert project.source_commit_sha == "c" * 40
        assert project.evidence_path
        assert read_manifest(project.evidence_path)["changes"]["modified"] == ["README.md"]
        assert not CatalogRepository(session).verified_skills()


def test_deleted_source_invalidates_skill(queue: QueueService) -> None:
    scan(queue, FakeGitHub(fixtures()))
    verify_all(queue)
    scan(
        queue,
        FakeGitHub(fixtures({"README.md": "# Robot\nDocumentation only.\n"}, commit="d" * 40)),
    )
    with queue.database.transaction() as session:
        assert not CatalogRepository(session).verified_skills()
        assert all(
            s.verification_status == "UNVERIFIED" for s in session.scalars(select(SkillCatalog))
        )


def test_truncated_tree_does_not_advance_catalog(queue: QueueService) -> None:
    scan(queue, FakeGitHub(fixtures()))
    data = fixtures(commit="e" * 40)
    data[f"/repos/{REPO}/git/trees/{TREE}?recursive=1"]["truncated"] = True
    task = queue.enqueue(TaskCreate("SCAN_GITHUB", payload={"repository": REPO}))
    asyncio.run(worker(queue, FakeGitHub(data)).run_once())
    assert queue.get(task).task_status == "FAILED"
    with queue.database.transaction() as session:
        assert session.scalars(select(ProjectCatalog)).one().source_commit_sha == COMMIT


def test_blob_integrity_failure_cannot_publish(queue: QueueService) -> None:
    data = fixtures()
    key = next(key for key in data if "/git/blobs/" in key)
    data[key]["content"] = base64.b64encode(b"forged").decode()
    task = queue.enqueue(TaskCreate("SCAN_GITHUB", payload={"repository": REPO}))
    asyncio.run(worker(queue, FakeGitHub(data)).run_once())
    assert queue.get(task).task_status == "FAILED"
    with queue.database.transaction() as session:
        assert not list(session.scalars(select(ProjectCatalog)))
        assert not list(session.scalars(select(CandidateFact)))


def test_candidate_evidence_replay_and_rejection(queue: QueueService) -> None:
    path, sha = store_evidence(
        queue.database.path.parent / "sources", b"Synthetic skill evidence: Python\n"
    )
    provenance = Provenance(
        source_type="LOCAL",
        source_reference="fixture-source-v1",
        path=path,
        sha256=sha,
        line_start=1,
        line_end=1,
        quote="Python",
    )
    with queue.database.transaction(immediate=True) as session:
        repo = CandidateFactRepository(session, queue.clock)
        data: dict[str, Any] = {
            "fact_type": "SKILL",
            "fact_key": "python",
            "statement": "Python",
            "provenance": provenance,
            "skill": "Python",
        }
        fact = repo.add(**data)
        assert repo.add(**data).fact_id == fact.fact_id
        repo.decide(
            fact.fact_id, "REJECTED", expected_status="UNVERIFIED", reviewer="fixture_reviewer"
        )
        assert not repo.verified()
        with pytest.raises(ValueError):
            repo.add(**{**data, "statement": "Invented metrics"})
    assert not retrieve(queue.database, "Python").facts


def test_tampered_evidence_excluded_even_if_marked_verified(queue: QueueService) -> None:
    from job_hunting_machine.security.paths import PathGuard

    scan(queue, FakeGitHub(fixtures()))
    verify_all(queue)
    with queue.database.transaction() as session:
        for fact in session.scalars(select(CandidateFact)):
            assert fact.evidence_path
            PathGuard().write_text(fact.evidence_path, "tampered")
    assert retrieve(queue.database, "Python robotics").facts == []


def test_inventory_is_paginated_and_deduplicated() -> None:
    rows = [{"full_name": "fixture/robot"}] * 100
    client = FakeGitHub(
        {
            "/users/fixture/repos?per_page=100&page=1&type=owner": rows,
            "/users/fixture/repos?per_page=100&page=2&type=owner": [
                {"full_name": "fixture/second"}
            ],
        }
    )
    assert asyncio.run(inventory(client, "fixture")) == ["fixture/robot", "fixture/second"]


def test_live_github_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ALLOW_LIVE", raising=False)
    with pytest.raises(ValueError):
        GitHubHTTP()


@pytest.mark.parametrize("after_publish", [False, True])
def test_crash_restart_reuses_snapshot(
    queue: QueueService, monkeypatch: pytest.MonkeyPatch, after_publish: bool
) -> None:
    client = FakeGitHub(fixtures())
    running = worker(queue, client)
    task = queue.enqueue(TaskCreate("SCAN_GITHUB", payload={"repository": REPO}))
    lease = queue.claim(["SCAN_GITHUB"])
    assert lease
    publish = running.scanner._publish

    def crash(*args: Any, **kwargs: Any) -> Any:
        if after_publish:
            publish(*args, **kwargs)
        raise SystemExit("simulated process death")

    monkeypatch.setattr(running.scanner, "_publish", crash)
    with pytest.raises(SystemExit):
        asyncio.run(running._execute(lease))
    calls = list(client.calls)
    queue.clock = FrozenClock(queue.clock.now() + timedelta(seconds=601))
    restarted = worker(queue, client)
    asyncio.run(restarted.startup())
    asyncio.run(restarted.run_once())
    assert queue.get(task).task_status == "SUCCEEDED"
    assert client.calls == calls
    with queue.database.transaction() as session:
        assert len(list(session.scalars(select(CandidateFact)))) == 5
        assert (
            len(
                list(
                    session.scalars(
                        select(ActivityLog).where(ActivityLog.event_type == "github_scan_published")
                    )
                )
            )
            == 1
        )


def test_stale_scan_cannot_overwrite_new_commit(queue: QueueService) -> None:
    from job_hunting_machine.orchestration.queue import RetryableError

    scan(queue, FakeGitHub(fixtures()))
    with queue.database.transaction() as session:
        project = session.scalars(select(ProjectCatalog)).one()
        assert project.evidence_path
        stale = read_manifest(project.evidence_path)
        stale_path = project.evidence_path
    scan(queue, FakeGitHub(fixtures(commit="c" * 40)))
    task = queue.enqueue(TaskCreate("SCAN_GITHUB", payload={"repository": REPO}))
    lease = queue.claim(["SCAN_GITHUB"])
    assert lease and lease.task_id == task
    with pytest.raises(RetryableError):
        worker(queue, FakeGitHub()).scanner._publish(lease, stale_path, stale)
    with queue.database.transaction() as session:
        assert session.scalars(select(ProjectCatalog)).one().source_commit_sha == "c" * 40


def test_scan_rolls_back_on_fact_failure(
    queue: QueueService, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = CandidateFactRepository.add
    count = 0

    def fail(self: CandidateFactRepository, **kwargs: Any) -> CandidateFact:
        nonlocal count
        result = original(self, **kwargs)
        count += 1
        if count == 2:
            raise RuntimeError("injected transaction failure")
        return result

    monkeypatch.setattr(CandidateFactRepository, "add", fail)
    task = queue.enqueue(TaskCreate("SCAN_GITHUB", payload={"repository": REPO}))
    asyncio.run(worker(queue, FakeGitHub(fixtures())).run_once())
    assert queue.get(task).task_status == "FAILED"
    with queue.database.transaction() as session:
        assert not list(session.scalars(select(CandidateFact)))
        assert not list(session.scalars(select(ProjectCatalog)))
        assert not list(session.scalars(select(SkillCatalog)))


def test_verified_ranking_uses_gateway_and_rejects_invented_ids(queue: QueueService) -> None:
    from job_hunting_machine.knowledge.retrieval import ranked_retrieval
    from job_hunting_machine.models.client import MockResponsesClient, ModelResponse
    from job_hunting_machine.models.gateway import ModelGateway
    from job_hunting_machine.models.pricing import TokenUsage

    scan(queue, FakeGitHub(fixtures()))
    scan(queue, FakeGitHub(fixtures(repo="fixture/other")), "fixture/other")
    verify_all(queue)
    task = queue.enqueue(TaskCreate("RANK_PROJECTS"))
    client = MockResponsesClient(
        [ModelResponse('{"project_ids":["invented"]}', TokenUsage(10, 0, 5))]
    )
    gateway = ModelGateway(queue.database, client=client, clock=queue.clock)
    result = asyncio.run(ranked_retrieval(queue.database, "Python", task_id=task, gateway=gateway))
    assert len(result.projects) == 2
    assert "invented" not in {p.project_id for p in result.projects}
    assert client.calls[0].model_id == "gpt-5.6-terra"
    assert "95%" not in client.calls[0].context  # Nonmatching metric excluded before model ranking.


def test_revocation_while_ranking_cannot_leak_fact(queue: QueueService) -> None:
    from job_hunting_machine.knowledge.retrieval import ranked_retrieval
    from job_hunting_machine.models.client import MockResponsesClient, ModelRequest, ModelResponse
    from job_hunting_machine.models.gateway import ModelGateway
    from job_hunting_machine.models.pricing import TokenUsage

    scan(queue, FakeGitHub(fixtures()))
    scan(queue, FakeGitHub(fixtures(repo="fixture/other")), "fixture/other")
    verify_all(queue)

    class RevokingClient(MockResponsesClient):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            with queue.database.transaction(immediate=True) as session:
                repo = CandidateFactRepository(session)
                for fact in repo.verified():
                    repo.decide(
                        fact.fact_id,
                        "REJECTED",
                        expected_status="VERIFIED",
                        reviewer="fixture_reviewer",
                    )
            return ModelResponse('{"project_ids":[]}', TokenUsage(10, 0, 5))

    task = queue.enqueue(TaskCreate("RANK_PROJECTS"))
    result = asyncio.run(
        ranked_retrieval(
            queue.database,
            "Python",
            task_id=task,
            gateway=ModelGateway(queue.database, client=RevokingClient(), clock=queue.clock),
        )
    )
    assert not result.facts and not result.projects


def test_api_transport_get_only_fixed_origin_no_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/collect"})

    api = GitHubHTTP(transport=httpx.MockTransport(respond))
    with pytest.raises(ValueError):
        asyncio.run(api.get("/repos/fixture/robot"))
    assert len(requests) == 1
    assert requests[0].url.host == "api.github.com"
    assert requests[0].method == "GET"
    assert "authorization" not in requests[0].headers


def test_reviewer_cannot_verify_wrong_or_stale_evidence(queue: QueueService) -> None:
    from job_hunting_machine.database.repositories import ConcurrentUpdateError

    scan(queue, FakeGitHub(fixtures()))
    with queue.database.transaction(immediate=True) as session:
        repo = CandidateFactRepository(session)
        fact = session.scalars(select(CandidateFact)).first()
        assert fact
        repo.decide(fact.fact_id, "REJECTED", expected_status="UNVERIFIED", reviewer="fixture")
        with pytest.raises(ConcurrentUpdateError):
            repo.decide(fact.fact_id, "VERIFIED", expected_status="UNVERIFIED", reviewer="fixture")
    scan(queue, FakeGitHub(fixtures(commit="d" * 40)))
    with queue.database.transaction(immediate=True) as session, pytest.raises(ValueError):
        CandidateFactRepository(session).decide(
            fact.fact_id, "VERIFIED", expected_status="REJECTED", reviewer="fixture"
        )


def test_evidence_quote_must_exist(queue: QueueService) -> None:
    path, sha = store_evidence(queue.database.path.parent / "sources", b"Measured 50 samples.\n")
    provenance = Provenance(
        source_type="LOCAL",
        source_reference="fixture",
        path=path,
        sha256=sha,
        line_start=1,
        line_end=1,
        quote="99% accuracy",
    )
    with queue.database.transaction() as session, pytest.raises(ValueError, match="quote"):
        CandidateFactRepository(session).add(
            fact_type="METRIC", fact_key="metric", statement="99% accuracy", provenance=provenance
        )


def test_review_cli_requires_exact_fact_hash(queue: QueueService) -> None:
    from typer.testing import CliRunner

    from job_hunting_machine.cli import app

    scan(queue, FakeGitHub(fixtures()))
    with queue.database.transaction() as session:
        fact = session.scalars(select(CandidateFact)).first()
        assert fact
        identity = fact.fact_id
    result = CliRunner().invoke(
        app,
        [
            "catalog",
            "decide",
            identity,
            "VERIFIED",
            "--value-sha256",
            "0" * 64,
            "--reviewer",
            "fixture",
            "--database",
            str(queue.database.path),
        ],
    )
    assert result.exit_code == 2
    with queue.database.transaction() as session:
        assert CandidateFactRepository(session).get(identity).verification_status == "UNVERIFIED"


def test_rate_limit_classified_retryable() -> None:
    import httpx

    from job_hunting_machine.orchestration.queue import RetryableError

    api = GitHubHTTP(transport=httpx.MockTransport(lambda request: httpx.Response(429)))
    with pytest.raises(RetryableError):
        asyncio.run(api.get("/repos/fixture/robot"))
