"""Phase 5 acceptance uses synthetic pages and real SQLite/LangGraph persistence."""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from job_hunting_machine.agents.extraction import extract
from job_hunting_machine.agents.fetch import FakeReader, JobFetcher, Page
from job_hunting_machine.agents.qualification import evaluate, policy_snapshot
from job_hunting_machine.agents.retrieve_links import canonicalize
from job_hunting_machine.agents.worker import QualificationWorker
from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    ActivityLog,
    AgentTask,
    ApplicationDetails,
    ApplicationPipeline,
    Job,
)
from job_hunting_machine.database.repositories import TaskCreate
from job_hunting_machine.database.seeds import seed_policies
from job_hunting_machine.orchestration import QueueService

URL = "https://careers.example.com/jobs/123"
NOW = "2026-09-05T12:00:00+00:00"


def posting(**changes: Any) -> str:
    data: dict[str, Any] = {
        "@type": "JobPosting",
        "title": "Software Engineering Internship",
        "hiringOrganization": {"name": "Synthetic Robotics"},
        "employmentType": "INTERN",
        "jobLocation": {
            "address": {"addressCountry": "US", "addressRegion": "MA", "addressLocality": "Amherst"}
        },
        "baseSalary": {
            "currency": "USD",
            "value": {"minValue": 25, "maxValue": 30, "unitText": "HOUR"},
        },
        "jobStartDate": "2027-06-01",
        "jobEndDate": "2027-08-20",
        "validThrough": "2027-03-01",
        "description": (
            "Paid internship. CPT accepted. Applications open. "
            "0 years professional experience required."
        ),
    }
    data.update(changes)
    return (
        '<html><script type="application/ld+json">'
        + json.dumps(data)
        + "</script><body><h1>"
        + str(data["title"])
        + "</h1></body></html>"
    )


@pytest.fixture
def queue(tmp_path: Path) -> Iterator[QueueService]:
    db = Database(tmp_path / "jobs.db")
    db.migrate()
    clock = FrozenClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
    with db.transaction() as session:
        seed_policies(session, clock)
    yield QueueService(db, clock=clock)
    db.dispose()


def run_job(
    queue: QueueService, html: str, *, status: int = 200, browser: FakeReader | None = None
) -> tuple[Job, FakeReader]:
    reader = FakeReader({URL: Page(URL, status, html)})
    worker = QualificationWorker(
        queue,
        fetcher=JobFetcher(reader, browser, evidence_root=queue.database.path.parent / "evidence"),
    )
    queue.enqueue(TaskCreate("RETRIEVE_LINKS", payload={"urls": [URL]}))

    async def run() -> None:
        await worker.startup()
        assert await worker.run_once()
        assert await worker.run_once()

    asyncio.run(run())
    with queue.database.transaction() as session:
        job = session.scalars(select(Job)).one()
        assert session.scalars(
            select(AgentTask).where(AgentTask.task_type == "QUALIFY_JOB")
        ).one().task_status in {"SUCCEEDED", "WAITING_HUMAN"}
        return job, reader


@pytest.mark.parametrize(
    ("changes", "expected", "failed_key"),
    [
        ({}, "PASSED", None),
        (
            {
                "description": (
                    "Unpaid internship. CPT accepted. Applications open. "
                    "0 years professional experience required."
                )
            },
            "ABORTED",
            "internship.paid",
        ),
        (
            {"jobLocation": {"address": {"addressCountry": "CA", "addressLocality": "Toronto"}}},
            "ABORTED",
            "internship.country",
        ),
        (
            {"baseSalary": {"currency": "USD", "value": {"value": 18, "unitText": "HOUR"}}},
            "ABORTED",
            "internship.salary",
        ),
        (
            {
                "jobLocation": {
                    "address": {
                        "addressCountry": "US",
                        "addressRegion": "CA",
                        "addressLocality": "San Francisco",
                    }
                },
                "baseSalary": {"currency": "USD", "value": {"value": 40, "unitText": "HOUR"}},
            },
            "PASSED",
            None,
        ),
        ({"experienceRequirements": "5+ years required"}, "ABORTED", "professional_experience"),
        ({"experienceRequirements": "3-5 years required"}, "PASSED", None),
        ({"validThrough": "2026-01-01"}, "ABORTED", "application_open"),
        (
            {"jobStartDate": "2028-06-01", "jobEndDate": "2028-08-01"},
            "ABORTED",
            "internship.period",
        ),
        (
            {
                "description": (
                    "Paid internship. Applications open. 0 years professional experience required."
                )
            },
            "NEEDS_REVIEW",
            None,
        ),
    ],
)
def test_internship_acceptance(
    queue: QueueService, changes: dict[str, Any], expected: str, failed_key: str | None
) -> None:
    job, reader = run_job(queue, posting(**changes))
    assert job.qualification_status == expected, job.notes
    assert reader.calls == [URL]
    assert job.raw_snapshot_path and Path(job.raw_snapshot_path).is_file()
    assert job.content_sha256
    if failed_key:
        assert any(
            r["key"] == failed_key and r["result"] == "FAIL"
            for r in json.loads(job.notes or "{}")["rules"]
        )
        assert json.loads(job.failed_rules_json or "[]")
    with queue.database.transaction() as session:
        count = len(list(session.scalars(select(ApplicationPipeline))))
        assert count == int(expected == "PASSED")
        assert len(list(session.scalars(select(ApplicationDetails)))) == count
        tasks = list(
            session.scalars(select(AgentTask).where(AgentTask.task_type == "BUILD_RESUME"))
        )
        assert len(tasks) == count
        assert all(t.task_status == "READY" for t in tasks)


@pytest.mark.parametrize(
    ("authorization", "expected"),
    [
        ("No visa sponsorship.", "ABORTED"),
        ("", "NEEDS_REVIEW"),
        ("Visa sponsorship available.", "PASSED"),
    ],
)
def test_full_time_authorization(queue: QueueService, authorization: str, expected: str) -> None:
    job, _ = run_job(
        queue,
        posting(
            title="Software Engineering New Graduate",
            employmentType="FULL_TIME",
            description=(
                f"Applications open. 2 years professional experience required. {authorization}"
            ),
        ),
    )
    assert job.qualification_status == expected, job.notes


def test_duplicate_urls_create_one_job_and_task(queue: QueueService) -> None:
    queue.enqueue(
        TaskCreate(
            "RETRIEVE_LINKS",
            payload={"urls": [f"<{URL}?utm_source=slack|job>", URL + "#apply", URL]},
        )
    )
    worker = QualificationWorker(queue)
    asyncio.run(worker.run_once())
    with queue.database.transaction() as session:
        job = session.scalars(select(Job)).one()
        assert job.qualification_status == "NEW"
        assert job.job_id.startswith("JOB_")
        assert (
            len(
                list(session.scalars(select(AgentTask).where(AgentTask.task_type == "QUALIFY_JOB")))
            )
            == 1
        )


def test_removed_page(queue: QueueService) -> None:
    job, _ = run_job(queue, "Job removed", status=404)
    assert job.qualification_status == "ABORTED"


def test_browser_fallback(queue: QueueService) -> None:
    browser = FakeReader({URL: Page(URL, 200, posting())})
    job, http = run_job(queue, "<html><div id='app'>Loading</div></html>", browser=browser)
    assert job.qualification_status == "PASSED"
    assert http.calls == browser.calls == [URL]
    assert len(list(Path(job.raw_snapshot_path or "").parent.glob("*.html"))) == 2


def test_challenge_never_bypassed(queue: QueueService) -> None:
    browser = FakeReader()
    job, _ = run_job(queue, "Verify you are human CAPTCHA", browser=browser)
    assert job.qualification_status == "NEEDS_REVIEW"
    assert not browser.calls


def test_policy_edit_changes_salary(queue: QueueService) -> None:
    with queue.database.transaction() as session:
        policy = policy_snapshot(session)
    for salary in policy["salaries"]:
        salary["minimum"] = 26
    assert evaluate(extract(posting()), policy, NOW).outcome == "FAIL"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/job",
        "https://example.com/job?token=secret",
        "https://user:pass@example.com/job",
        "http://localhost/job",
    ],
)
def test_unsafe_url_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        canonicalize(url)


def test_canonicalization_preserves_identity() -> None:
    assert (
        canonicalize("https://EXAMPLE.com:443/%7Ejobs?utm_source=x&jobId=12#apply")
        == "https://example.com/~jobs?jobId=12"
    )
    assert canonicalize("https://example.com/jobs?jobId=12") != canonicalize(
        "https://example.com/jobs?jobId=13"
    )


def test_crash_after_evidence_resumes_without_refetch(
    queue: QueueService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from job_hunting_machine.orchestration.queue import Lease

    reader = FakeReader({URL: Page(URL, 200, posting())})
    fetcher = JobFetcher(reader, evidence_root=queue.database.path.parent / "evidence")
    worker = QualificationWorker(queue, fetcher=fetcher)
    queue.enqueue(TaskCreate("RETRIEVE_LINKS", payload={"urls": [URL]}))
    asyncio.run(worker.run_once())
    lease = queue.claim(["QUALIFY_JOB"])
    assert lease
    original = worker._store

    def crash(lease: Lease, memory: dict[str, object], data: dict[str, Any]) -> None:
        original(lease, memory, data)
        raise SystemExit("simulated process death")

    monkeypatch.setattr(worker, "_store", crash)
    with pytest.raises(SystemExit):
        asyncio.run(worker._execute(lease))
    assert queue.get(lease.task_id).task_status == "ACTIVE"
    queue.clock = FrozenClock(queue.clock.now() + timedelta(seconds=601))
    restarted = QualificationWorker(queue, fetcher=fetcher)

    async def resume() -> None:
        assert await restarted.startup() == 1
        assert await restarted.run_once()

    asyncio.run(resume())
    assert reader.calls == [URL]
    assert queue.get(lease.task_id).task_status == "SUCCEEDED"
    with queue.database.transaction() as session:
        assert session.scalars(select(Job)).one().qualification_status == "PASSED"
        assert len(list(session.scalars(select(ApplicationPipeline)))) == 1


def test_application_failure_rolls_back_entire_decision(
    queue: QueueService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_hunting_machine.database.repositories import ApplicationRepository

    original = ApplicationRepository.create_from_passed_job

    def fail(self: ApplicationRepository, *args: Any, **kwargs: Any) -> Any:
        original(self, *args, **kwargs)
        raise RuntimeError("simulated after application creation")

    monkeypatch.setattr(ApplicationRepository, "create_from_passed_job", fail)
    reader = FakeReader({URL: Page(URL, 200, posting())})
    worker = QualificationWorker(
        queue, fetcher=JobFetcher(reader, evidence_root=queue.database.path.parent / "evidence")
    )
    queue.enqueue(TaskCreate("RETRIEVE_LINKS", payload={"urls": [URL]}))
    asyncio.run(worker.run_once())
    asyncio.run(worker.run_once())
    with queue.database.transaction() as session:
        assert session.scalars(select(Job)).one().qualification_status == "ACTIVE"
        assert not list(session.scalars(select(ApplicationPipeline)))
        assert not list(session.scalars(select(ApplicationDetails)))
        assert not list(
            session.scalars(select(AgentTask).where(AgentTask.task_type == "BUILD_RESUME"))
        )
        assert not list(
            session.scalars(
                select(ActivityLog).where(ActivityLog.event_type == "qualification_decided")
            )
        )


def test_crash_after_publish_does_not_duplicate_application(
    queue: QueueService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    reader = FakeReader({URL: Page(URL, 200, posting())})
    fetcher = JobFetcher(reader, evidence_root=queue.database.path.parent / "evidence")
    worker = QualificationWorker(queue, fetcher=fetcher)
    queue.enqueue(TaskCreate("RETRIEVE_LINKS", payload={"urls": [URL]}))
    asyncio.run(worker.run_once())
    lease = queue.claim(["QUALIFY_JOB"])
    assert lease
    original = worker._publish

    def crash(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise SystemExit("after publish")

    monkeypatch.setattr(worker, "_publish", crash)
    with pytest.raises(SystemExit):
        asyncio.run(worker._execute(lease))
    queue.clock = FrozenClock(queue.clock.now() + timedelta(seconds=601))
    restarted = QualificationWorker(queue, fetcher=fetcher)
    asyncio.run(restarted.startup())
    asyncio.run(restarted.run_once())
    assert queue.get(lease.task_id).task_status == "SUCCEEDED"
    assert reader.calls == [URL]
    with queue.database.transaction() as session:
        assert len(list(session.scalars(select(ApplicationPipeline)))) == 1
        assert (
            len(
                list(
                    session.scalars(
                        select(ActivityLog).where(ActivityLog.event_type == "qualification_decided")
                    )
                )
            )
            == 1
        )


def test_semantic_gateway_escalates_with_evidence(queue: QueueService) -> None:
    from job_hunting_machine.agents.qualification import semantic_check
    from job_hunting_machine.models.client import MockResponsesClient, ModelResponse
    from job_hunting_machine.models.gateway import ModelGateway
    from job_hunting_machine.models.pricing import TokenUsage

    text = "Our placement is compatible with CPT."
    facts = extract(
        posting(description=f"Paid internship. Applications open. No experience required. {text}")
    )
    with queue.database.transaction() as session:
        policy = policy_snapshot(session)
    task_id = queue.enqueue(TaskCreate("QUALIFY_JOB"))
    responses = [
        ModelResponse(
            json.dumps(
                {
                    "findings": [
                        {
                            "field": "cpt",
                            "value": "SUPPORTED",
                            "quote": text,
                            "confidence": confidence,
                        }
                    ]
                }
            ),
            TokenUsage(10, 0, 10),
        )
        for confidence in (0.5, 0.98)
    ]
    client = MockResponsesClient(list(responses))
    gateway = ModelGateway(queue.database, client=client, clock=queue.clock)
    result = asyncio.run(semantic_check(gateway, task_id, facts, evaluate(facts, policy, NOW)))
    assert result.cpt == "SUPPORTED"
    assert [request.model_id for request in client.calls] == ["gpt-5.6-luna", "gpt-5.6-terra"]


def test_semantic_cannot_invent_sponsorship(queue: QueueService) -> None:
    from job_hunting_machine.agents.qualification import semantic_check
    from job_hunting_machine.models.client import MockResponsesClient, ModelResponse
    from job_hunting_machine.models.gateway import ModelGateway
    from job_hunting_machine.models.pricing import TokenUsage

    facts = extract(posting(title="Software Engineering New Graduate", employmentType="FULL_TIME"))
    with queue.database.transaction() as session:
        policy = policy_snapshot(session)
    task_id = queue.enqueue(TaskCreate("QUALIFY_JOB"))
    client = MockResponsesClient(
        [
            ModelResponse(
                json.dumps(
                    {
                        "findings": [
                            {
                                "field": "authorization",
                                "value": "SUPPORTED",
                                "quote": "Applications open.",
                                "confidence": 0.99,
                            }
                        ]
                    }
                ),
                TokenUsage(10, 0, 10),
            )
        ]
    )
    gateway = ModelGateway(queue.database, client=client, clock=queue.clock)
    result = asyncio.run(semantic_check(gateway, task_id, facts, evaluate(facts, policy, NOW)))
    assert result.authorization == "UNKNOWN"
    assert evaluate(result, policy, NOW).outcome == "REVIEW"


def test_html_parser_without_jsonld(queue: QueueService) -> None:
    html = """<h1>Software Engineering Internship</h1><p>Company: Synthetic Robotics;
    Country: US; City: Amherst; State: MA; Start date: 2027-06-01;
    End date: 2027-08-01; Deadline: 2027-03-01; Paid internship, $25/hour.
    No experience required. CPT accepted. Applications open.</p>"""
    job, _ = run_job(queue, html)
    assert job.qualification_status == "PASSED", job.notes


def test_live_fetch_requires_explicit_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from job_hunting_machine.agents.fetch import HTTPReader, PlaywrightReader

    monkeypatch.delenv("JOB_FETCH_ALLOW_LIVE", raising=False)
    monkeypatch.delenv("JOB_BROWSER_ALLOW_LIVE", raising=False)
    with pytest.raises(ValueError):
        HTTPReader()
    with pytest.raises(ValueError):
        PlaywrightReader()


def test_http_reader_pins_address_and_preserves_tls_host(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from job_hunting_machine.agents import fetch

    monkeypatch.setenv("JOB_FETCH_ALLOW_LIVE", "1")
    requests: list[httpx.Request] = []

    async def resolve(url: str) -> tuple[str, str]:
        return url, "93.184.216.34"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=posting(), headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(fetch, "resolved_url", resolve)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    result = asyncio.run(fetch.HTTPReader().read(URL))
    assert result.url == URL
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["Host"] == "careers.example.com"
    assert requests[0].extensions["sni_hostname"] == b"careers.example.com"
    assert requests[0].method == "GET"


def test_http_redirect_cannot_reach_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from job_hunting_machine.agents import fetch

    monkeypatch.setenv("JOB_FETCH_ALLOW_LIVE", "1")

    async def resolve(url: str) -> tuple[str, str]:
        return canonicalize(url), "93.184.216.34"

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        )
    )
    monkeypatch.setattr(fetch, "resolved_url", resolve)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(ValueError):
        asyncio.run(fetch.HTTPReader().read(URL))


def test_high_cost_city_alias_cannot_use_standard_threshold(queue: QueueService) -> None:
    job, _ = run_job(
        queue,
        posting(
            jobLocation={
                "address": {
                    "addressCountry": "US",
                    "addressRegion": "NY",
                    "addressLocality": "New York",
                }
            }
        ),
    )
    assert job.qualification_status == "ABORTED"


def test_deadline_time_boundary(queue: QueueService) -> None:
    with queue.database.transaction() as session:
        policy = policy_snapshot(session)
    facts = extract(posting())
    assert evaluate(facts, policy, NOW).outcome == "PASS"
    assert evaluate(facts, policy, "2027-04-01T00:00:00+00:00").outcome == "FAIL"
