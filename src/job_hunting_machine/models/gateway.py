"""The sole model-call boundary: route, reserve, request, record, validate, then retry."""

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable
from enum import StrEnum

from pydantic import BaseModel

from job_hunting_machine.clock import Clock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.models.budgets import BudgetExceeded, BudgetLedger, UsageContext
from job_hunting_machine.models.client import (
    ClientError,
    MockResponsesClient,
    ModelRequest,
    ResponseClient,
    _live_client,
)
from job_hunting_machine.models.pricing import TokenUsage, estimate_cost
from job_hunting_machine.models.prompts import PromptRegistry, digest, load_prompts
from job_hunting_machine.models.router import Registry, Tier, load_registry
from job_hunting_machine.models.schemas import (
    StructuredValidationError,
    strict_schema,
    validate_output,
)
from job_hunting_machine.orchestration.checkpoints import run_blocking


class EscalationReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    HARD_RULE_UNKNOWN = "hard_rule_unknown"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    REQUIRED_FIELD_UNRESOLVED = "required_field_unresolved"
    PARSER_DISAGREEMENT = "parser_disagreement"


class ModelGatewayError(RuntimeError):
    """Safe diagnostic token; never includes prompts, output, credentials or provider text."""


class ModelGateway:
    def __init__(
        self,
        database: Database,
        *,
        registry: Registry | None = None,
        prompts: PromptRegistry | None = None,
        client: ResponseClient | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.registry = (registry or load_registry()).model_copy(deep=True)
        self.prompts = (prompts or load_prompts()).model_copy(deep=True)
        self.client = client if client is not None else MockResponsesClient()
        self.ledger = BudgetLedger(database, self.registry.budgets, clock=clock)
        self.sleep = sleep
        self.jitter = jitter

    @classmethod
    def for_openai(
        cls,
        database: Database,
        *,
        registry: Registry | None = None,
        prompts: PromptRegistry | None = None,
    ) -> "ModelGateway":
        """Explicit live transport factory; environment opt-in and API key are both required."""
        # Validate local configuration before constructing any SDK/network resources.
        gateway = cls(database, registry=registry, prompts=prompts)
        gateway.client = _live_client()
        return gateway

    async def close(self) -> None:
        await self.client.close()

    async def _delay(self, attempt: int) -> None:
        delays = self.registry.policy.retry_delays_seconds
        fraction = self.jitter()
        if not 0 <= fraction <= 1:
            raise ValueError("Jitter must be between zero and one")
        await self.sleep(delays[min(attempt - 1, len(delays) - 1)] * (1 + 0.2 * fraction))

    async def structured[T: BaseModel](
        self,
        *,
        task_id: str,
        operation: str,
        agent_name: str,
        context: str,
        output_type: type[T],
        prompt_name: str = "structured_response",
        prompt_version: str = "1.0.0",
        ceiling: Tier | None = None,
        allow_fallback: bool = False,
        cache_namespace: str | None = "jhm",
        quality_check: Callable[[T], EscalationReason | None] | None = None,
    ) -> T:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", agent_name):
            raise ValueError("Agent name must be a static identifier")
        policy = self.registry.policy
        steps = self.registry.route(operation, ceiling=ceiling, allow_fallback=allow_fallback)
        prompt = self.prompts.get(prompt_name, prompt_version)
        schema = strict_schema(output_type)
        # Text-only upper estimate: UTF-8 bytes + schema + conservative envelope overhead.
        # Never assume cached input when admitting a request.
        input_bound = (
            len(prompt.instructions.encode())
            + len(context.encode())
            + len(json.dumps(schema, ensure_ascii=False).encode())
            + 1024
        )
        if input_bound > policy.max_input_tokens:
            raise BudgetExceeded("Input exceeds the configured conservative token bound")
        prompt_hash = digest({"prompt": prompt.sha256, "context": context, "schema": schema})
        request_count = 0
        for step in steps:
            schema_errors = transport_errors = 0
            while request_count < policy.max_requests:
                model = self.registry.models[step.model]
                if not model.enabled:
                    raise ModelGatewayError("model_disabled")
                maximum = estimate_cost(
                    model,
                    TokenUsage(
                        input_bound,
                        0,
                        policy.max_output_tokens,
                        input_bound if cache_namespace is not None else 0,
                    ),
                )
                request = ModelRequest(
                    model.model_id,
                    step.reasoning,
                    prompt.instructions,
                    context,
                    schema,
                    policy.max_output_tokens,
                    prompt.cache_key(schema, cache_namespace)
                    if cache_namespace is not None
                    else None,
                )
                reservation = await run_blocking(
                    self.ledger.reserve,
                    UsageContext(
                        task_id,
                        agent_name,
                        operation,
                        model.model_id,
                        step.reasoning,
                        prompt.version,
                        prompt_hash,
                        self.client.mode,
                        prompt.name,
                        prompt.sha256,
                    ),
                    maximum,
                )
                request_count += 1
                try:
                    response = await self.client.generate(request)
                except ClientError as error:
                    if error.definitely_unbilled:
                        await run_blocking(
                            self.ledger.settle,
                            reservation,
                            TokenUsage(0, 0, 0),
                            maximum * 0,
                            response_id=None,
                            success=False,
                        )
                    # Ambiguous transport/cancellation leaves the full reservation charged.
                    transport_errors += 1
                    if (
                        not error.retryable
                        or transport_errors >= policy.transport_attempts_per_model
                    ):
                        raise ModelGatewayError("transport_attempts_exhausted") from None
                    if request_count < policy.max_requests:
                        await self._delay(transport_errors)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise ModelGatewayError("unexpected_transport_failure") from None
                if response.usage is None:
                    raise ModelGatewayError("unmetered_response")
                result: T | None = None
                validation_bug = False
                if response.status == "completed" and not response.refused:
                    try:
                        result = validate_output(response.text, output_type, schema)
                    except StructuredValidationError:
                        pass
                    except Exception:
                        validation_bug = True
                cost = estimate_cost(model, response.usage)
                await run_blocking(
                    self.ledger.settle,
                    reservation,
                    response.usage,
                    cost,
                    response_id=response.response_id,
                    success=result is not None,
                )
                if cost > maximum:
                    raise BudgetExceeded(
                        "Actual usage exceeded its reservation; further calls stopped"
                    )
                if response.refused:
                    raise ModelGatewayError("model_refused")
                if validation_bug:
                    raise ModelGatewayError("output_validator_failed")
                if result is not None:
                    reason = quality_check(result) if quality_check else None
                    if reason is None:
                        return result
                    if not isinstance(reason, EscalationReason):
                        raise ValueError("Quality checks must return an explicit escalation reason")
                    break
                schema_errors += 1
                if schema_errors >= policy.schema_attempts_per_model:
                    break
                if request_count < policy.max_requests:
                    await self._delay(schema_errors)
        raise ModelGatewayError("structured_attempts_exhausted")
