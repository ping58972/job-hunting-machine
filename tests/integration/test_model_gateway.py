"""Phase 3 acceptance: isolated SQLite budgets and mocked model responses only."""

import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import Field, ValidationError
from sqlalchemy import select

from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import ModelUsage
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationRepository,
    JobCreate,
    JobRepository,
    TaskCreate,
    TaskRepository,
)
from job_hunting_machine.models import (
    BudgetExceeded,
    EscalationReason,
    MockResponsesClient,
    ModelGateway,
    ModelGatewayError,
    ModelResponse,
    StructuredOutput,
    Tier,
    TokenUsage,
    estimate_cost,
    load_registry,
)
from job_hunting_machine.models.budgets import BudgetLedger, UsageContext
from job_hunting_machine.models.client import ClientError, _OpenAIResponsesClient
from job_hunting_machine.models.prompts import load_prompts
from job_hunting_machine.models.router import Registry, RoutingError

CLOCK = FrozenClock(datetime(2026, 9, 5, 12, tzinfo=UTC))


class Answer(StructuredOutput):
    value: str
    confidence: float = Field(ge=0, le=1)


async def no_wait(seconds: float) -> None:
    assert seconds >= 0


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "models.db")
    db.migrate()
    yield db
    db.dispose()


def task(
    database: Database, task_type: str = "QUALIFY_JOB", application_id: str | None = None
) -> str:
    with database.transaction() as session:
        return (
            TaskRepository(session, CLOCK)
            .create(TaskCreate(task_type, application_id=application_id))
            .task_id
        )


def response(text: str = '{"value":"synthetic","confidence":0.95}', **kwargs: Any) -> ModelResponse:
    return ModelResponse(text, TokenUsage(100, 20, 10), "resp_mock", **kwargs)


def configured(**changes: Any) -> Registry:
    values = load_registry().model_dump(mode="json")
    for key, value in changes.items():
        values[key].update(value)
    return Registry.model_validate(values)


def gateway(
    database: Database, replies: list[ModelResponse | ClientError], **kwargs: Any
) -> ModelGateway:
    return ModelGateway(
        database,
        client=MockResponsesClient(replies),
        clock=CLOCK,
        sleep=no_wait,
        jitter=lambda: 0,
        **kwargs,
    )


def call(gw: ModelGateway, task_id: str, **kwargs: Any) -> Answer:
    return asyncio.run(
        gw.structured(
            task_id=task_id,
            operation="qualification",
            agent_name="fixture",
            context="Synthetic input only",
            output_type=Answer,
            **kwargs,
        )
    )


def calls(gw: ModelGateway) -> list[str]:
    assert isinstance(gw.client, MockResponsesClient)
    return [request.model_id for request in gw.client.calls]


def usage(database: Database) -> list[ModelUsage]:
    with database.transaction() as session:
        return list(session.scalars(select(ModelUsage)))


def test_correct_model_reasoning_and_usage_persisted(database: Database) -> None:
    identifier = task(database)
    gw = gateway(database, [response()])
    assert call(gw, identifier).value == "synthetic"
    assert calls(gw) == ["gpt-5.6-luna"]
    assert isinstance(gw.client, MockResponsesClient)
    assert gw.client.calls[0].reasoning == "low"
    (row,) = usage(database)
    assert (row.task_id, row.agent_name, row.operation) == (identifier, "fixture", "qualification")
    assert (row.input_tokens, row.cached_input_tokens, row.output_tokens) == (100, 20, 10)
    assert row.success == 1 and row.response_id == "resp_mock"
    assert row.prompt_version == "1.0.0"
    assert row.prompt_sha256 and len(row.prompt_sha256) == 64
    assert row.created_at == "2026-09-05T12:00:00.000Z"
    assert row.estimated_cost_usd == pytest.approx(0.0000284)


def test_estimated_cost_cached_input_and_reasoning_counted_once() -> None:
    model = load_registry().models[Tier.TERRA]
    # 800 uncached * $2/M + 200 cached * $0.2/M + 300 output * $12/M.
    assert estimate_cost(model, TokenUsage(1000, 200, 300)) == Decimal("0.00524")
    assert estimate_cost(model, TokenUsage(0, 0, 0)) == 0
    with pytest.raises(ValueError):
        TokenUsage(10, 11, 1)


def test_budget_blocks_before_transport(database: Database) -> None:
    gw = gateway(database, [response()], registry=configured(budgets={"daily_usd": "0.00001"}))
    with pytest.raises(BudgetExceeded):
        call(gw, task(database))
    assert calls(gw) == []
    assert usage(database) == []


def test_invalid_schema_twice_escalates_luna_to_terra(database: Database) -> None:
    gw = gateway(database, [response("not json"), response('{"wrong":true}'), response()])
    assert call(gw, task(database)).confidence == 0.95
    assert calls(gw) == ["gpt-5.6-luna", "gpt-5.6-luna", "gpt-5.6-terra"]
    assert [row.success for row in usage(database)] == [0, 0, 1]
    assert all(row.estimated_cost_usd and row.estimated_cost_usd > 0 for row in usage(database))


def test_ceiling_bounds_schema_retries(database: Database) -> None:
    gw = gateway(database, [response("broken")] * 10)
    with pytest.raises(ModelGatewayError, match="structured_attempts_exhausted"):
        call(gw, task(database), ceiling=Tier.LUNA)
    assert calls(gw) == ["gpt-5.6-luna"] * 2


def test_astra_disabled_never_selected(database: Database) -> None:
    values = load_registry().model_dump(mode="json")
    values["policy"]["escalation_ceiling"] = "astra"
    values["routes"]["qualification"]["final_escalation"] = {"model": "astra", "reasoning": "high"}
    registry = Registry.model_validate(values)
    assert not registry.models[Tier.ASTRA].enabled
    gw = gateway(database, [response("broken")] * 8, registry=registry)
    with pytest.raises(ModelGatewayError):
        call(gw, task(database), ceiling=Tier.ASTRA)
    assert calls(gw) == ["gpt-5.6-luna"] * 2 + ["gpt-5.6-terra"] * 2


def test_schema_attempts_bounded_by_overall_request_limit(database: Database) -> None:
    gw = gateway(
        database, [response("broken")] * 10, registry=configured(policy={"max_requests": 3})
    )
    with pytest.raises(ModelGatewayError):
        call(gw, task(database))
    assert len(calls(gw)) == 3


def test_explicit_quality_reason_can_escalate(database: Database) -> None:
    gw = gateway(database, [response(), response()])
    checks = iter([EscalationReason.LOW_CONFIDENCE, None])
    call(gw, task(database), quality_check=lambda _: next(checks))
    assert calls(gw) == ["gpt-5.6-luna", "gpt-5.6-terra"]


def test_unknown_task_type_and_missing_task_fail_before_call(database: Database) -> None:
    from job_hunting_machine.ids import IdGenerator

    gw = gateway(database, [response()])
    with pytest.raises(BudgetExceeded):
        call(gw, task(database, "UNKNOWN"))
    with pytest.raises(ValueError, match="persisted task"):
        call(gw, IdGenerator().task_id())
    assert calls(gw) == []


def test_default_mock_never_constructs_live_client(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_ALLOW_LIVE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-not-real")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Live client must not be constructed")

    monkeypatch.setattr("job_hunting_machine.models.client.AsyncOpenAI", forbidden)
    gw = ModelGateway(database, sleep=no_wait)
    with pytest.raises(ModelGatewayError):
        call(gw, task(database))
    assert gw.client.mode == "mock"
    assert usage(database)[0].estimated_cost_usd == 0


def test_live_factory_requires_explicit_environment_flag(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_ALLOW_LIVE", raising=False)
    with pytest.raises(ValueError, match="OPENAI_ALLOW_LIVE"):
        ModelGateway.for_openai(database)


def reservation_context(task_id: str) -> UsageContext:
    return UsageContext(
        task_id, "fixture", "qualification", "gpt-5.6-luna", "low", "fixture:1", "a" * 64, "mock"
    )


def test_atomic_reservations_prevent_concurrent_overspend(database: Database) -> None:
    identifier = task(database)
    ledger = BudgetLedger(database, load_registry().budgets, clock=CLOCK)
    barrier = Barrier(2)

    def reserve() -> str | None:
        barrier.wait()
        try:
            return ledger.reserve(reservation_context(identifier), Decimal("0.02"))
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(value is not None for value in results) == 1
    assert len(usage(database)) == 1


def test_pending_reservation_survives_restart_and_calendar_rollover(database: Database) -> None:
    identifier = task(database)
    limits = configured(budgets={"daily_usd": ".025", "monthly_usd": ".025"}).budgets
    first = BudgetLedger(database, limits, clock=CLOCK)
    reservation = first.reserve(reservation_context(identifier), Decimal(".02"))
    reopened = Database(database.path)
    try:
        new = BudgetLedger(reopened, limits, clock=FrozenClock(datetime(2026, 10, 1, tzinfo=UTC)))
        with pytest.raises(BudgetExceeded):
            new.reserve(reservation_context(task(database)), Decimal(".02"))
        new.settle(
            reservation,
            TokenUsage(100, 0, 10),
            Decimal(".000032"),
            response_id="mock",
            success=True,
        )
        assert new.reserve(reservation_context(task(database)), Decimal(".02"))
    finally:
        reopened.dispose()


def test_application_budget_covers_separate_tasks(database: Database) -> None:
    with database.transaction() as session:
        job = JobRepository(session, CLOCK).create(
            JobCreate(
                original_url="https://example.invalid/gateway",
                canonical_url="https://example.invalid/gateway",
                source_type="TEST",
                company_name="Synthetic",
                job_title="Synthetic",
                qualification_status="PASSED",
            )
        )
        application = ApplicationRepository(session, CLOCK).create_from_passed_job(
            job.job_id, ApplicationCreate("Synthetic", "Synthetic", job.canonical_url)
        )
    first = task(database, application_id=application.application_id)
    second = task(database, application_id=application.application_id)
    limits = configured(budgets={"per_application_usd": ".025"}).budgets
    ledger = BudgetLedger(database, limits, clock=CLOCK)
    ledger.reserve(reservation_context(first), Decimal(".02"))
    with pytest.raises(BudgetExceeded):
        ledger.reserve(reservation_context(second), Decimal(".02"))
    assert usage(database)[0].application_id == application.application_id


@pytest.mark.parametrize("limit", ["daily_usd", "monthly_usd"])
def test_global_budget_covers_separate_tasks(database: Database, limit: str) -> None:
    ledger = BudgetLedger(database, configured(budgets={limit: ".025"}).budgets, clock=CLOCK)
    ledger.reserve(reservation_context(task(database)), Decimal(".02"))
    with pytest.raises(BudgetExceeded):
        ledger.reserve(reservation_context(task(database)), Decimal(".02"))


def test_transport_retry_recording_and_no_tier_escalation(database: Database) -> None:
    gw = gateway(database, [ClientError(retryable=True), ClientError(retryable=True), response()])
    with pytest.raises(ModelGatewayError, match="transport_attempts"):
        call(gw, task(database))
    assert calls(gw) == ["gpt-5.6-luna"] * 2
    assert all(row.input_tokens is None and row.estimated_cost_usd for row in usage(database))


def test_rate_limit_without_charge_can_retry(database: Database) -> None:
    gw = gateway(database, [ClientError(retryable=True, definitely_unbilled=True), response()])
    call(gw, task(database))
    assert len(calls(gw)) == 2
    assert usage(database)[0].estimated_cost_usd == 0


def test_refusal_records_usage_and_does_not_escalate(database: Database) -> None:
    gw = gateway(database, [response("", refused=True), response()])
    with pytest.raises(ModelGatewayError, match="refused"):
        call(gw, task(database))
    assert len(calls(gw)) == 1
    assert usage(database)[0].success == 0
    assert usage(database)[0].input_tokens == 100


def test_missing_usage_keeps_conservative_reservation(database: Database) -> None:
    gw = gateway(database, [ModelResponse('{"value":"x","confidence":1.0}', None)])
    with pytest.raises(ModelGatewayError, match="unmetered"):
        call(gw, task(database))
    assert usage(database)[0].input_tokens is None
    assert usage(database)[0].estimated_cost_usd


def test_prompt_hash_tracks_context_but_cache_key_is_stable(database: Database) -> None:
    gw = gateway(database, [response(), response()])
    identifier = task(database)
    call(gw, identifier)
    asyncio.run(
        gw.structured(
            task_id=identifier,
            operation="qualification",
            agent_name="fixture",
            context="Different synthetic context",
            output_type=Answer,
        )
    )
    rows = usage(database)
    assert rows[0].prompt_sha256 != rows[1].prompt_sha256
    assert isinstance(gw.client, MockResponsesClient)
    assert gw.client.calls[0].prompt_cache_key == gw.client.calls[1].prompt_cache_key
    prompt = load_prompts().get("structured_response", "1.0.0")
    assert len(prompt.sha256) == 64
    assert prompt.cache_key({}) != prompt.cache_key({"changed": True})


def test_deterministic_route_requires_explicit_fallback() -> None:
    registry = load_registry()
    with pytest.raises(RoutingError):
        registry.route("job_html_extraction")
    assert registry.route("job_html_extraction", allow_fallback=True)[0].reasoning == "none"


def test_invalid_registry_rejected() -> None:
    with pytest.raises(ValidationError):
        configured(models={"luna": {"model_id": "bad", "input_per_million_usd": -1}})
    with pytest.raises(ValidationError):
        configured(routes={"bad": {"primary": {"model": "terra", "reasoning": "ultra"}}})


def test_responses_api_payload_and_parsing_using_mock_http(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    logging.getLogger("openai").setLevel(logging.DEBUG)
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "resp_http_mock",
                "object": "response",
                "created_at": 1788609600,
                "status": "completed",
                "model": body["model"],
                "output": [
                    {
                        "type": "message",
                        "id": "msg_mock",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"value":"sdk","confidence":0.95}',
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 0},
                    "output_tokens": 10,
                    "output_tokens_details": {"reasoning_tokens": 4},
                    "total_tokens": 110,
                },
            },
        )

    async def run() -> Answer:
        sdk = AsyncOpenAI(
            api_key="synthetic",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        gw = ModelGateway(database, client=_OpenAIResponsesClient(sdk), sleep=no_wait)
        try:
            return await gw.structured(
                task_id=task(database),
                operation="qualification",
                agent_name="fixture",
                context="Synthetic SDK test",
                output_type=Answer,
            )
        finally:
            await gw.close()

    assert asyncio.run(run()).value == "sdk"
    (body,) = requests
    assert body["store"] is False and body["service_tier"] == "default"
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_output_tokens"] == 1024
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"]["additionalProperties"] is False
    assert len(body["prompt_cache_key"]) == 64
    assert usage(database)[0].response_id == "resp_http_mock"
    assert "Synthetic SDK test" not in caplog.text


def test_cache_write_price_is_included_without_double_counting() -> None:
    model = load_registry().models[Tier.TERRA]
    # 400 ordinary input, 200 cached input, 400 cache writes, 300 output.
    assert estimate_cost(model, TokenUsage(1000, 200, 300, 400)) == Decimal(".00544")
    with pytest.raises(ValueError):
        TokenUsage(100, 90, 10, 11)


def test_cache_write_usage_is_recorded_in_audit(database: Database) -> None:
    from job_hunting_machine.database.models import ActivityLog

    gw = gateway(
        database,
        [
            ModelResponse(
                '{"value":"cache","confidence":0.9}', TokenUsage(100, 20, 10, 50), "mock_cache"
            )
        ],
    )
    call(gw, task(database))
    assert usage(database)[0].estimated_cost_usd == pytest.approx(0.0000309)
    with database.transaction() as session:
        event = session.scalar(
            select(ActivityLog).where(ActivityLog.event_type == "model_usage_recorded")
        )
        assert event and event.metadata_json
        assert json.loads(event.metadata_json)["cache_write_tokens"] == 50


def test_strict_schema_rejects_defaults_missing_from_response() -> None:
    from job_hunting_machine.models.schemas import (
        StructuredValidationError,
        strict_schema,
        validate_output,
    )

    class WithDefault(StructuredOutput):
        answer: str = "default"

    schema = strict_schema(WithDefault)
    with pytest.raises(StructuredValidationError):
        validate_output("{}", WithDefault, schema)
    assert validate_output('{"answer":"provided"}', WithDefault, schema).answer == "provided"


@pytest.mark.parametrize(
    "payload",
    [
        '{"value":"x","confidence":NaN}',
        '{"value":"x","confidence":"0.9"}',
        '{"value":"x","confidence":0.9,"extra":true}',
        '{"value":"x","confidence":1.5}',
    ],
)
def test_pydantic_and_json_schema_both_validate(payload: str) -> None:
    from job_hunting_machine.models.schemas import (
        StructuredValidationError,
        strict_schema,
        validate_output,
    )

    with pytest.raises(StructuredValidationError):
        validate_output(payload, Answer, strict_schema(Answer))


def test_structured_schema_cannot_fetch_remote_references() -> None:
    from pydantic import ConfigDict

    from job_hunting_machine.models.schemas import strict_schema

    class RemoteSchema(StructuredOutput):
        model_config = ConfigDict(json_schema_extra={"$ref": "https://example.invalid/schema"})
        value: str

    with pytest.raises(ValueError, match="local schema"):
        strict_schema(RemoteSchema)


def test_prompt_versions_are_semantic_and_unique() -> None:
    from job_hunting_machine.models.prompts import Prompt, PromptRegistry

    with pytest.raises(ValidationError):
        Prompt(name="fixture", version="1", instructions="fixture")
    prompt = Prompt(name="fixture", version="1.2.3", instructions="fixture")
    with pytest.raises(ValidationError):
        PromptRegistry(version=1, prompts=(prompt, prompt))


def test_astra_missing_enabled_flag_remains_disabled() -> None:
    values = load_registry().model_dump(mode="json")
    values["models"]["astra"].pop("enabled")
    registry = Registry.model_validate(values)
    assert not registry.models[Tier.ASTRA].enabled


def test_configuration_ceiling_cannot_be_raised_by_caller(database: Database) -> None:
    registry = configured(policy={"escalation_ceiling": "luna"})
    gw = gateway(database, [response("bad")] * 5, registry=registry)
    with pytest.raises(ModelGatewayError):
        call(gw, task(database), ceiling=Tier.ASTRA)
    assert calls(gw) == ["gpt-5.6-luna"] * 2


def test_output_overrun_persists_actual_cost_then_stops(database: Database) -> None:
    gw = gateway(
        database,
        [
            ModelResponse(
                '{"value":"x","confidence":0.9}', TokenUsage(1000000, 0, 1000000), "mock_overrun"
            )
        ],
    )
    with pytest.raises(BudgetExceeded, match="Actual usage"):
        call(gw, task(database))
    assert usage(database)[0].estimated_cost_usd == pytest.approx(1.4)
    assert len(calls(gw)) == 1


def test_canceled_request_keeps_durable_budget_reservation(database: Database) -> None:
    class WaitingClient(MockResponsesClient):
        async def generate(self, request: Any) -> ModelResponse:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("Unreachable")

    async def scenario() -> None:
        gw = ModelGateway(database, client=WaitingClient())
        request = asyncio.create_task(
            gw.structured(
                task_id=task(database),
                operation="qualification",
                agent_name="fixture",
                context="Synthetic",
                output_type=Answer,
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    entered = asyncio.Event()
    asyncio.run(scenario())
    (row,) = usage(database)
    assert row.input_tokens is None and row.estimated_cost_usd and row.estimated_cost_usd > 0


def test_unpriced_historical_usage_blocks_further_spending(database: Database) -> None:
    identifier = task(database)
    ledger = BudgetLedger(database, load_registry().budgets, clock=CLOCK)
    reservation = ledger.reserve(reservation_context(identifier), Decimal(".01"))
    with database.transaction() as session:
        row = session.get(ModelUsage, reservation)
        assert row
        row.estimated_cost_usd = None
    with pytest.raises(BudgetExceeded, match="Unpriced"):
        ledger.reserve(reservation_context(task(database)), Decimal(".01"))


def test_openai_sdk_imports_are_confined_to_gateway_transport() -> None:
    import ast

    from job_hunting_machine.security.paths import PROJECT_ROOT

    package = PROJECT_ROOT / "src/job_hunting_machine"
    for path in package.rglob("*.py"):
        if path == package / "models/client.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("openai") for alias in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("openai"), path


def test_model_configuration_cli_is_read_only() -> None:
    from typer.testing import CliRunner

    from job_hunting_machine.cli import app

    result = CliRunner().invoke(app, ["models"])
    assert result.exit_code == 0, result.output
    values = json.loads(result.stdout)
    assert values["models"]["astra"]["enabled"] is False
    assert values["budgets"]["per_application_usd"] == "1.00"
