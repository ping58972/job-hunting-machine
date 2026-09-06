"""Read-only operational reports from the authoritative application database."""

from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from job_hunting_machine.clock import Clock, SystemClock, format_utc
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    AgentTask,
    ApplicationDetails,
    ApplicationPipeline,
    Approval,
    ExternalAction,
    ModelUsage,
)


class OperationalReports:
    def __init__(self, database: Database, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    def queue(self) -> dict[str, Any]:
        now = format_utc(self.clock.now())
        with self.database.transaction() as session:
            rows = list(session.scalars(select(AgentTask)))
        by_status = Counter(row.task_status for row in rows)
        by_type = Counter(row.task_type for row in rows)
        stale = sum(
            row.task_status == "ACTIVE"
            and (row.lease_expires_at is None or row.lease_expires_at <= now)
            for row in rows
        )
        return {
            "total": len(rows),
            "by_status": dict(sorted(by_status.items())),
            "by_type": dict(sorted(by_type.items())),
            "stale_active": stale,
            "waiting_human": by_status["WAITING_HUMAN"],
            "due_retries": sum(
                row.task_status == "WAITING_RETRY"
                and row.next_run_at is not None
                and row.next_run_at <= now
                for row in rows
            ),
        }

    def applications(self, *, limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValueError("application_report_limit_invalid")
        with self.database.transaction() as session:
            rows = list(
                session.execute(
                    select(ApplicationPipeline, ApplicationDetails)
                    .join(
                        ApplicationDetails,
                        ApplicationDetails.application_id == ApplicationPipeline.application_id,
                    )
                    .order_by(ApplicationPipeline.updated_at.desc())
                    .limit(limit)
                )
            )
            total = session.scalar(select(func.count()).select_from(ApplicationPipeline)) or 0
        counts = Counter(app.application_status for app, _ in rows)
        return {
            "total": total,
            "returned": len(rows),
            "by_status_in_result": dict(sorted(counts.items())),
            "applications": [
                {
                    "application_id": app.application_id,
                    "company": details.company_name,
                    "position": details.job_title,
                    "stage": app.pipeline_stage,
                    "status": app.application_status,
                    "updated_at": app.updated_at,
                }
                for app, details in rows
            ],
        }

    def costs(self) -> dict[str, Any]:
        day = format_utc(self.clock.now() - timedelta(days=1))
        month = self.clock.now().strftime("%Y-%m-01T00:00:00.000Z")
        with self.database.transaction() as session:
            rows = list(session.scalars(select(ModelUsage)))
        settled = [row for row in rows if row.input_tokens is not None]
        reserved = [row for row in rows if row.input_tokens is None]

        def total(items: list[ModelUsage]) -> float:
            return round(sum(row.estimated_cost_usd or 0.0 for row in items), 8)

        by_model: dict[str, float] = {}
        by_operation: dict[str, float] = {}
        for row in rows:
            by_model[row.model_id] = by_model.get(row.model_id, 0.0) + (
                row.estimated_cost_usd or 0.0
            )
            by_operation[row.operation] = by_operation.get(row.operation, 0.0) + (
                row.estimated_cost_usd or 0.0
            )
        return {
            "currency": "USD",
            "records": len(rows),
            "estimated_total": total(rows),
            "settled_total": total(settled),
            "unresolved_reservations": len(reserved),
            "unresolved_reserved_total": total(reserved),
            "last_24_hours": total([row for row in rows if row.created_at >= day]),
            "current_month": total([row for row in rows if row.created_at >= month]),
            "by_model": {key: round(value, 8) for key, value in sorted(by_model.items())},
            "by_operation": {key: round(value, 8) for key, value in sorted(by_operation.items())},
        }

    def status(self, runtime_mode: str) -> dict[str, Any]:
        queue = self.queue()
        with self.database.transaction() as session:
            applications = (
                session.scalar(select(func.count()).select_from(ApplicationPipeline)) or 0
            )
            unknown_actions = (
                session.scalar(
                    select(func.count())
                    .select_from(ExternalAction)
                    .where(ExternalAction.action_status == "UNKNOWN_RESULT")
                )
                or 0
            )
            pending_approvals = (
                session.scalar(
                    select(func.count())
                    .select_from(Approval)
                    .where(Approval.approval_status == "PENDING")
                )
                or 0
            )
        return {
            "runtime_mode": runtime_mode,
            "database": str(self.database.path),
            "health": "ATTENTION" if queue["stale_active"] or unknown_actions else "OK",
            "applications": applications,
            "queue": queue,
            "pending_approvals": pending_approvals,
            "unknown_external_actions": unknown_actions,
            "live_enabled": runtime_mode == "LIVE",
        }
