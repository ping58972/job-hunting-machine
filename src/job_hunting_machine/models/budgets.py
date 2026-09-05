"""Durable, atomic budget admission using the architecture's existing model_usage table.

An unsettled row has NULL tokens and conservatively holds its maximum estimate.
Crashes/unknown transport outcomes never silently free that reservation.
"""

import math
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import false, or_, select

from job_hunting_machine.clock import Clock, SystemClock, format_utc
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import AgentTask, ModelUsage
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.ids import IdGenerator, IdKind, validate_id
from job_hunting_machine.models.pricing import TokenUsage
from job_hunting_machine.models.router import Budgets


class BudgetExceeded(RuntimeError):
    """The requested maximum cost exceeds a durable spending ceiling."""


@dataclass(frozen=True)
class UsageContext:
    task_id: str
    agent_name: str
    operation: str
    model_id: str
    reasoning: str
    prompt_version: str
    prompt_sha256: str
    mode: str
    prompt_name: str | None = None
    template_sha256: str | None = None


def stored_cost(value: Decimal) -> float:
    # Architecture specifies REAL; round upwards so a float cannot weaken a ceiling.
    result = float(value)
    return math.nextafter(result, math.inf) if Decimal(str(result)) < value else result


class BudgetLedger:
    def __init__(self, database: Database, limits: Budgets, *, clock: Clock | None = None) -> None:
        self.database = database
        self.limits = limits.model_copy(deep=True)
        self.clock = clock or SystemClock()
        self.ids = IdGenerator(self.clock)

    def reserve(self, context: UsageContext, maximum: Decimal) -> str:
        if not maximum.is_finite() or maximum < 0:
            raise ValueError("Invalid reservation estimate")
        with self.database.transaction(immediate=True) as session:
            task = session.get(AgentTask, validate_id(context.task_id, IdKind.TASK))
            if task is None:
                raise ValueError("Model requests require a persisted task")
            limit = self.limits.per_task_usd.get(task.task_type)
            if limit is None:
                raise BudgetExceeded("Task type has no explicit model budget")
            stamp = format_utc(self.clock.now())
            today, month = stamp[:10], stamp[:7]
            # Pending reservations from older periods remain counted until resolved.
            rows = session.scalars(
                select(ModelUsage).where(
                    or_(
                        ModelUsage.task_id == task.task_id,
                        ModelUsage.application_id == task.application_id
                        if task.application_id
                        else false(),
                        ModelUsage.created_at >= f"{month}-01T00:00:00.000Z",
                        ModelUsage.input_tokens.is_(None),
                    )
                )
            )
            task_spend = app_spend = daily = monthly = Decimal(0)
            for row in rows:
                if (
                    row.estimated_cost_usd is None
                    or not math.isfinite(row.estimated_cost_usd)
                    or row.estimated_cost_usd < 0
                ):
                    raise BudgetExceeded("Unpriced historical usage requires reconciliation")
                cost = Decimal(str(row.estimated_cost_usd))
                if row.task_id == task.task_id:
                    task_spend += cost
                if task.application_id and row.application_id == task.application_id:
                    app_spend += cost
                pending = row.input_tokens is None
                if pending or row.created_at[:10] >= today:
                    daily += cost
                if pending or row.created_at[:7] >= month:
                    monthly += cost
            ceilings = [
                (task_spend, limit),
                (daily, self.limits.daily_usd),
                (monthly, self.limits.monthly_usd),
            ]
            if task.application_id:
                ceilings.append((app_spend, self.limits.per_application_usd))
            if any(spent + maximum > ceiling for spent, ceiling in ceilings):
                raise BudgetExceeded("Model request exceeds a configured spending ceiling")
            usage_id = self.ids.generate_ulid()
            session.add(
                ModelUsage(
                    model_usage_id=usage_id,
                    task_id=task.task_id,
                    application_id=task.application_id,
                    agent_name=context.agent_name,
                    operation=context.operation,
                    model_id=context.model_id,
                    reasoning_effort=context.reasoning,
                    prompt_version=context.prompt_version,
                    prompt_sha256=context.prompt_sha256,
                    estimated_cost_usd=stored_cost(maximum),
                    success=0,
                    created_at=stamp,
                )
            )
            session.flush()
            ActivityLogRepository(session, self.clock, self.ids).append(
                ActivityEvent(
                    "model_budget_reserved",
                    task_id=task.task_id,
                    application_id=task.application_id,
                    metadata={
                        "usage_id": usage_id,
                        "maximum_usd": str(maximum),
                        "mode": context.mode,
                        "prompt_name": context.prompt_name,
                        "template_sha256": context.template_sha256,
                    },
                )
            )
            return usage_id

    def settle(
        self,
        usage_id: str,
        usage: TokenUsage,
        cost: Decimal,
        *,
        response_id: str | None,
        success: bool,
    ) -> None:
        if not cost.is_finite() or cost < 0:
            raise ValueError("Invalid usage cost")
        with self.database.transaction(immediate=True) as session:
            row = session.get(ModelUsage, usage_id)
            if row is None or row.input_tokens is not None:
                raise ValueError("Usage reservation is missing or already settled")
            maximum = Decimal(str(row.estimated_cost_usd))
            row.input_tokens = usage.input_tokens
            row.cached_input_tokens = usage.cached_input_tokens
            row.output_tokens = usage.output_tokens
            row.estimated_cost_usd = stored_cost(cost)
            row.response_id = response_id
            row.success = int(success)
            ActivityLogRepository(session, self.clock, self.ids).append(
                ActivityEvent(
                    "model_usage_recorded",
                    task_id=row.task_id,
                    application_id=row.application_id,
                    metadata={
                        "usage_id": usage_id,
                        "success": success,
                        "cost_usd": str(cost),
                        "exceeded_reservation": cost > maximum,
                        "cache_write_tokens": usage.cache_write_tokens,
                    },
                )
            )
