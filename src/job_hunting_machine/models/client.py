"""Transport adapters, invoked exclusively by ModelGateway. No implicit SDK retries."""

import logging
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from job_hunting_machine.models.pricing import TokenUsage
from job_hunting_machine.models.router import Effort


@dataclass(frozen=True)
class ModelRequest:
    model_id: str
    reasoning: Effort
    instructions: str = field(repr=False)
    context: str = field(repr=False)
    schema: dict[str, Any] = field(repr=False)
    max_output_tokens: int
    prompt_cache_key: str | None


@dataclass(frozen=True)
class ModelResponse:
    text: str = field(repr=False)
    usage: TokenUsage | None
    response_id: str | None = None
    status: str = "completed"
    refused: bool = False


class ClientError(RuntimeError):
    def __init__(self, *, retryable: bool, definitely_unbilled: bool = False) -> None:
        super().__init__("Model transport failed")
        self.retryable = retryable
        self.definitely_unbilled = definitely_unbilled


class ResponseClient(Protocol):
    mode: Literal["mock", "openai"]

    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def close(self) -> None: ...


class MockResponsesClient:
    """Scripted deterministic results. Exhaustion fails; it never falls back to a network."""

    mode: Literal["mock", "openai"] = "mock"

    def __init__(self, responses: list[ModelResponse | ClientError] | None = None) -> None:
        self.responses = deque(responses or [])
        self.calls: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if not self.responses:
            raise ClientError(retryable=False, definitely_unbilled=True)
        result = self.responses.popleft()
        if isinstance(result, ClientError):
            raise result
        return result

    async def close(self) -> None:
        pass


class _OpenAIResponsesClient:
    """Private transport. Construct through ModelGateway.for_openai; tests inject HTTP mocks."""

    mode: Literal["mock", "openai"] = "openai"

    def __init__(self, sdk: AsyncOpenAI) -> None:
        self._sdk = sdk
        # OPENAI_LOG=debug otherwise exposes request bodies through SDK diagnostics.
        sdk_logger = logging.getLogger("openai")
        sdk_logger.setLevel(logging.CRITICAL + 1)
        sdk_logger.propagate = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        try:
            response = await self._sdk.responses.create(
                model=request.model_id,
                instructions=request.instructions,
                input=request.context,
                reasoning={"effort": request.reasoning},
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "structured_response",
                        "schema": request.schema,
                        "strict": True,
                    }
                },
                max_output_tokens=request.max_output_tokens,
                prompt_cache_key=request.prompt_cache_key,
                prompt_cache_options={
                    "mode": "implicit" if request.prompt_cache_key else "explicit"
                },
                store=False,
                service_tier="default",
                truncation="disabled",
            )
        except APIConnectionError:
            raise ClientError(retryable=True) from None
        except APIStatusError as error:
            status = error.status_code
            raise ClientError(
                retryable=status in {408, 409, 429} or status >= 500,
                definitely_unbilled=400 <= status < 500 and status != 408,
            ) from None
        usage = response.usage
        cache_writes = (
            getattr(usage.input_tokens_details, "cache_write_tokens", None) if usage else None
        )
        if usage and cache_writes is None:
            # Missing billing detail must not undercount cache-write charges.
            cache_writes = usage.input_tokens - usage.input_tokens_details.cached_tokens
        return ModelResponse(
            text=response.output_text,
            usage=TokenUsage(
                usage.input_tokens,
                usage.input_tokens_details.cached_tokens,
                usage.output_tokens,
                cache_writes or 0,
            )
            if usage
            else None,
            response_id=response.id,
            status=response.status or "unknown",
            refused=any(
                part.type == "refusal"
                for item in response.output
                if item.type == "message"
                for part in item.content
            ),
        )

    async def close(self) -> None:
        await self._sdk.close()


def _live_client() -> ResponseClient:
    if os.environ.get("OPENAI_ALLOW_LIVE") != "1":
        raise ValueError("Live model transport requires OPENAI_ALLOW_LIVE=1")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("Live model transport requires OPENAI_API_KEY")
    return _OpenAIResponsesClient(
        AsyncOpenAI(
            api_key=key,
            base_url="https://api.openai.com/v1",
            max_retries=0,
            timeout=30,
            http_client=httpx.AsyncClient(trust_env=False, timeout=30),
        )
    )
