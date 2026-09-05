"""Validated Architecture v2 registry; routing definitions do not implement agents."""

import itertools
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_hunting_machine.config import ConfigurationError
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard

Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class Tier(StrEnum):
    LUNA = "luna"
    TERRA = "terra"
    SOL = "sol"
    ASTRA = "astra"

    @property
    def rank(self) -> int:
        return list(Tier).index(self)


type Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ModelDefinition(Settings):
    model_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9.\-]+$")
    enabled: bool = True
    input_per_million_usd: Money
    cached_input_per_million_usd: Money
    output_per_million_usd: Money
    cache_write_per_million_usd: Money | None = None
    reasoning_efforts: tuple[Effort, ...] = ("none", "low", "medium", "high")

    @model_validator(mode="after")
    def prices(self) -> Self:
        if self.cached_input_per_million_usd > self.input_per_million_usd:
            raise ValueError("Cached price must not exceed ordinary input price")
        if self.cache_write_price < self.input_per_million_usd:
            raise ValueError("Cache write price cannot be lower than ordinary input")
        return self

    @property
    def cache_write_price(self) -> Decimal:
        # Current GPT-5.6+ cache writes cost 1.25x input. YAML can override this rate.
        return (
            self.cache_write_per_million_usd
            if self.cache_write_per_million_usd is not None
            else self.input_per_million_usd * Decimal("1.25")
        )


class Selection(Settings):
    model: Tier
    reasoning: Effort


class Route(Settings):
    primary: Selection | Literal["deterministic"]
    fallback: Selection | None = None
    escalation: Selection | None = None
    final_escalation: Selection | None = None

    def steps(self, *, allow_fallback: bool = False) -> tuple[Selection, ...]:
        if self.primary == "deterministic":
            if not allow_fallback or self.fallback is None:
                raise RoutingError("Deterministic route requires explicit fallback intent")
            first = self.fallback
        else:
            first = self.primary
        return (first, *tuple(s for s in (self.escalation, self.final_escalation) if s))


class Budgets(Settings):
    per_task_usd: dict[str, Money]
    per_application_usd: Money
    daily_usd: Money
    monthly_usd: Money


class Policy(Settings):
    escalation_ceiling: Tier = Tier.SOL
    schema_attempts_per_model: int = Field(default=2, ge=2, le=5)
    transport_attempts_per_model: int = Field(default=2, ge=1, le=5)
    max_requests: int = Field(default=6, ge=1, le=20)
    max_output_tokens: int = Field(default=1024, ge=16, le=32768)
    max_input_tokens: int = Field(default=16000, ge=1, le=200000)
    retry_delays_seconds: tuple[Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)], ...] = (
        1,
        2,
        4,
        8,
    )


class Registry(Settings):
    version: Literal[1]
    models: dict[Tier, ModelDefinition]
    routes: dict[str, Route]
    budgets: Budgets
    confidence: dict[str, Annotated[float, Field(ge=0, le=1)]]
    policy: Policy = Field(default_factory=Policy)

    @model_validator(mode="after")
    def check_routes(self) -> Self:
        if set(self.models) != set(Tier):
            raise ValueError("All four architecture tiers are required")
        if "enabled" not in self.models[Tier.ASTRA].model_fields_set:
            self.models[Tier.ASTRA] = self.models[Tier.ASTRA].model_copy(update={"enabled": False})
        if not self.policy.retry_delays_seconds:
            raise ValueError("Retry delays cannot be empty")
        for route in self.routes.values():
            steps = route.steps(allow_fallback=True)
            if any(a.model.rank >= b.model.rank for a, b in itertools.pairwise(steps)):
                raise ValueError("Escalation must advance to a higher tier")
            for step in steps:
                if step.reasoning not in self.models[step.model].reasoning_efforts:
                    raise ValueError("Unsupported reasoning effort")
        return self

    def route(
        self, operation: str, *, ceiling: Tier | None = None, allow_fallback: bool = False
    ) -> tuple[Selection, ...]:
        if operation not in self.routes:
            raise RoutingError("Operation has no registered route")
        limit = self.policy.escalation_ceiling.rank
        if ceiling is not None:
            limit = min(limit, ceiling.rank)
        steps = self.routes[operation].steps(allow_fallback=allow_fallback)
        selected: list[Selection] = []
        for step in steps:
            # Never skip a disabled tier to silently select a more expensive one.
            if step.model.rank > limit or not self.models[step.model].enabled:
                break
            selected.append(step)
        if not selected:
            raise RoutingError("Primary model is disabled or above the escalation ceiling")
        return tuple(selected)


class RoutingError(ValueError):
    """The requested route cannot be used under the configured policy."""


def load_registry(path: Path = PROJECT_ROOT / "config/models.yaml") -> Registry:
    try:
        content = PathGuard().validate_write(path).read_text(encoding="utf-8")
        return Registry.model_validate(yaml.safe_load(content))
    except (ValueError, OSError, yaml.YAMLError) as error:
        raise ConfigurationError("Invalid root-local model registry") from error
