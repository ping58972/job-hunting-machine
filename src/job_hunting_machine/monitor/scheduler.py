"""Rate-limited selection of due, non-terminal applications."""

from datetime import timedelta

from sqlalchemy import select

from job_hunting_machine.clock import Clock, SystemClock, format_utc, unix_milliseconds
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import AgentTask, ApplicationDetails, ApplicationPipeline
from job_hunting_machine.database.repositories import TaskCreate, TaskRepository
from job_hunting_machine.ids import IdGenerator
from job_hunting_machine.monitor.config import MonitorSettings
from job_hunting_machine.monitor.types import TERMINAL_STATUSES

_PENDING_TASKS = frozenset({"NEW", "READY", "ACTIVE", "WAITING_HUMAN", "WAITING_RETRY"})


class MonitorScheduler:
    def __init__(
        self,
        database: Database,
        settings: MonitorSettings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.clock = clock or SystemClock()
        self.ids = IdGenerator(self.clock)

    def schedule_due(self) -> list[str]:
        scheduled: list[str] = []
        with self.database.transaction(immediate=True) as session:
            rows = list(
                session.execute(
                    select(ApplicationPipeline, ApplicationDetails)
                    .join(
                        ApplicationDetails,
                        ApplicationDetails.application_id == ApplicationPipeline.application_id,
                    )
                    .where(~ApplicationPipeline.application_status.in_(TERMINAL_STATUSES))
                    .order_by(
                        ApplicationPipeline.priority,
                        ApplicationPipeline.updated_at,
                        ApplicationPipeline.application_id,
                    )
                )
            )
            portal_budget = self.settings.max_portal_checks_per_run
            now = self.clock.now()
            for app, details in rows:
                checks = self.settings.checks_per_day(app.application_status)
                if checks == 0 or portal_budget == 0:
                    continue
                pending = session.scalar(
                    select(AgentTask.task_id).where(
                        AgentTask.application_id == app.application_id,
                        AgentTask.task_type == "MONITOR_APPLICATION",
                        AgentTask.task_status.in_(_PENDING_TASKS),
                    )
                )
                if pending:
                    continue
                interval = timedelta(seconds=86_400 / checks)
                if (
                    details.last_status_checked_at is not None
                    and details.last_status_checked_at > format_utc(now - interval)
                ):
                    continue
                bucket_ms = int(interval.total_seconds() * 1000)
                bucket = unix_milliseconds(now) // bucket_ms
                task = TaskRepository(session, self.clock, self.ids).create(
                    TaskCreate(
                        "MONITOR_APPLICATION",
                        task_status="READY",
                        application_id=app.application_id,
                        job_id=app.job_id,
                        dedupe_key=f"monitor:{app.application_id}:{bucket}",
                        payload={"application_id": app.application_id},
                        max_attempts=len(self.settings.retry_delays_seconds) + 1,
                    )
                )
                scheduled.append(task.task_id)
                portal_budget -= 1
        return scheduled
