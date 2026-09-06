"""Short, audited queue transactions. No application business-state mutations."""

import json
import random
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from job_hunting_machine.clock import Clock, SystemClock, format_utc
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import AgentTask, TaskMemory
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import (
    ConcurrentUpdateError,
    RecordNotFoundError,
)
from job_hunting_machine.database.repositories.tasks import TaskCreate, TaskRepository
from job_hunting_machine.ids import IdGenerator, IdKind, validate_id


class LeaseLostError(ConcurrentUpdateError):
    """This attempt no longer owns a live lease."""


class RetryableError(RuntimeError):
    """An explicitly classified transient node failure; message is never persisted."""


@dataclass(frozen=True)
class Lease:
    task_id: str
    worker_id: str
    attempt: int
    task_type: str
    payload: dict[str, object]


@dataclass(frozen=True)
class RetryPolicy:
    delays: tuple[float, ...] = (30, 120, 600, 1800)
    jitter: Callable[[], float] = random.random

    def delay(self, attempt: int) -> float:
        if not self.delays or any(value <= 0 for value in self.delays):
            raise ValueError("Retry delays must be positive")
        fraction = self.jitter()
        if not 0 <= fraction <= 1:
            raise ValueError("Retry jitter must be between zero and one")
        return self.delays[min(max(attempt - 1, 0), len(self.delays) - 1)] * (1 + 0.2 * fraction)


def encode_memory(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 65536:
        raise ValueError("Task memory must remain concise (64 KiB maximum)")
    return encoded


class QueueService:
    def __init__(
        self,
        database: Database,
        *,
        clock: Clock | None = None,
        lease_seconds: float = 600,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        self.database = database
        self.clock = clock or SystemClock()
        self.ids = IdGenerator(self.clock)
        self.lease_seconds = lease_seconds
        self.retry_policy = retry_policy or RetryPolicy()

    def _get(self, session: Session, task_id: str) -> AgentTask:
        row = session.get(AgentTask, validate_id(task_id, IdKind.TASK))
        if row is None:
            raise RecordNotFoundError("Task does not exist")
        return row

    def get(self, task_id: str) -> AgentTask:
        with self.database.transaction() as session:
            return self._get(session, task_id)

    def _audit(self, session: Session, row: AgentTask, event: str, old: str) -> None:
        row.updated_at = format_utc(self.clock.now())
        # Explicitly advance even when a FrozenClock produces the same heartbeat timestamp.
        row.version += 1
        session.flush()
        ActivityLogRepository(session, self.clock, self.ids).append(
            ActivityEvent(
                event,
                task_id=row.task_id,
                application_id=row.application_id,
                job_id=row.job_id,
                old_state=old,
                new_state=row.task_status,
                metadata={"attempt": row.attempt_count, "version": row.version},
            )
        )

    def enqueue(self, data: TaskCreate) -> str:
        if data.task_status not in {"NEW", "READY"}:
            raise ValueError("New queue tasks must be NEW or READY")
        with self.database.transaction(immediate=True) as session:
            row = TaskRepository(session, self.clock, self.ids).create(
                replace(data, task_status="NEW")
            )
            if row.task_status == "NEW":
                row.task_status = "READY"
                self._audit(session, row, "task_ready", "NEW")
            return row.task_id

    def claim(self, task_types: Sequence[str]) -> Lease | None:
        if not task_types:
            return None
        with self.database.transaction(immediate=True) as session:
            now = self.clock.now()
            stamp = format_utc(now)
            row = session.scalar(
                select(AgentTask)
                .where(
                    AgentTask.task_status == "READY",
                    AgentTask.task_type.in_(task_types),
                    or_(AgentTask.next_run_at.is_(None), AgentTask.next_run_at <= stamp),
                    or_(AgentTask.lease_expires_at.is_(None), AgentTask.lease_expires_at <= stamp),
                )
                .order_by(AgentTask.priority, AgentTask.created_at, AgentTask.task_id)
                .limit(1)
            )
            if row is None:
                return None
            stored = session.get(TaskMemory, row.task_id)
            memory = json.loads(stored.state_json) if stored else {}
            continuation = memory.pop("continuation", False) is True
            if row.attempt_count >= row.max_attempts and not continuation:
                self._finish(session, row, "FAILED", "attempts_exhausted")
                return None
            row.task_status = "ACTIVE"
            row.worker_id = self.ids.generate_ulid()  # Unique ownership token for every claim.
            if not continuation:
                row.attempt_count += 1
            else:
                self._memory(session, row, memory)
            row.heartbeat_at = stamp
            row.lease_expires_at = format_utc(now + timedelta(seconds=self.lease_seconds))
            row.started_at = row.started_at or stamp
            row.next_run_at = None
            row.checkpoint_key = row.task_id
            self._audit(session, row, "task_claimed", "READY")
            return Lease(
                row.task_id,
                row.worker_id,
                row.attempt_count,
                row.task_type,
                json.loads(row.payload_json or "{}"),
            )

    @contextmanager
    def fence(self, lease: Lease) -> Iterator[Session]:
        """Hold the writer reservation while validating and committing attempt-owned work.

        Checkpoint writes hold this fence until the separate saver commit completes.
        Recovery therefore cannot transfer ownership in the middle of a checkpoint write.
        """
        with self.database.transaction(immediate=True) as session:
            row = self._get(session, lease.task_id)
            if (
                row.task_status != "ACTIVE"
                or row.worker_id != lease.worker_id
                or row.attempt_count != lease.attempt
                or row.lease_expires_at is None
                or row.lease_expires_at <= format_utc(self.clock.now())
            ):
                raise LeaseLostError("Task lease expired or ownership changed")
            yield session

    def heartbeat(self, lease: Lease) -> None:
        with self.fence(lease) as session:
            row = self._get(session, lease.task_id)
            now = self.clock.now()
            row.heartbeat_at = format_utc(now)
            row.lease_expires_at = format_utc(now + timedelta(seconds=self.lease_seconds))
            self._audit(session, row, "task_heartbeat", "ACTIVE")

    def memory(self, task_id: str) -> dict[str, object]:
        with self.database.transaction() as session:
            self._get(session, task_id)
            row = session.get(TaskMemory, task_id)
            return json.loads(row.state_json) if row else {}

    def _memory(self, session: Session, row: AgentTask, value: Mapping[str, object]) -> None:
        encoded = encode_memory(value)
        memory = session.get(TaskMemory, row.task_id)
        if memory is None:
            memory = TaskMemory(task_id=row.task_id, memory_version=1)
            session.add(memory)
        else:
            memory.memory_version += 1
        memory.state_json = encoded
        memory.summary = "Durable workflow execution context"
        memory.last_agent = "workflow_runtime"
        memory.last_checkpoint = str(value.get("checkpoint_id") or "") or None
        memory.updated_at = format_utc(self.clock.now())

    def save_memory(self, lease: Lease, value: Mapping[str, object]) -> None:
        with self.fence(lease) as session:
            row = self._get(session, lease.task_id)
            self._memory(session, row, value)
            self._audit(session, row, "task_memory_saved", "ACTIVE")

    def _finish(self, session: Session, row: AgentTask, status: str, event: str) -> None:
        old = row.task_status
        row.task_status = status
        row.worker_id = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        if status in {"SUCCEEDED", "FAILED", "ABORTED", "CANCELED"}:
            row.completed_at = format_utc(self.clock.now())
            row.next_run_at = None
        self._audit(session, row, event, old)

    def complete(
        self, lease: Lease, memory: Mapping[str, object], *, waiting: bool = False
    ) -> None:
        with self.fence(lease) as session:
            self.complete_in_transaction(session, lease, memory, waiting=waiting)

    def complete_in_transaction(
        self,
        session: Session,
        lease: Lease,
        memory: Mapping[str, object],
        *,
        waiting: bool = False,
    ) -> None:
        """Finish an owned task inside its caller's already-fenced transaction."""
        row = self._get(session, lease.task_id)
        if (
            row.task_status != "ACTIVE"
            or row.worker_id != lease.worker_id
            or row.attempt_count != lease.attempt
        ):
            raise LeaseLostError("Task lease ownership changed")
        self._memory(session, row, memory)
        self._finish(
            session,
            row,
            "WAITING_HUMAN" if waiting else "SUCCEEDED",
            "task_waiting_human" if waiting else "task_succeeded",
        )

    def fail(self, lease: Lease, *, retryable: bool) -> None:
        with self.fence(lease) as session:
            row = self._get(session, lease.task_id)
            retry = retryable and row.attempt_count < row.max_attempts
            row.last_error_code = "RETRYABLE_ERROR" if retryable else "WORKFLOW_ERROR"
            row.last_error_message = None  # Exceptions may contain candidate data or secrets.
            if retry:
                row.next_run_at = format_utc(
                    self.clock.now() + timedelta(seconds=self.retry_policy.delay(row.attempt_count))
                )
            self._finish(
                session, row, "WAITING_RETRY" if retry else "FAILED", "task_failed_attempt"
            )

    def release(self, lease: Lease) -> None:
        with self.fence(lease) as session:
            row = self._get(session, lease.task_id)
            stored = session.get(TaskMemory, row.task_id)
            memory = json.loads(stored.state_json) if stored else {}
            memory["continuation"] = True
            self._memory(session, row, memory)
            self._finish(session, row, "READY", "task_shutdown_released")

    def recover(self) -> int:
        """Startup and periodic recovery; never reset live leases or human waits."""
        with self.database.transaction(immediate=True) as session:
            now = format_utc(self.clock.now())
            rows = list(
                session.scalars(
                    select(AgentTask).where(
                        or_(
                            (AgentTask.task_status == "ACTIVE")
                            & or_(
                                AgentTask.lease_expires_at.is_(None),
                                AgentTask.lease_expires_at <= now,
                            ),
                            (AgentTask.task_status == "WAITING_RETRY")
                            & (AgentTask.next_run_at <= now),
                        )
                    )
                )
            )
            for row in rows:
                status = "FAILED" if row.attempt_count >= row.max_attempts else "READY"
                row.next_run_at = None
                self._finish(session, row, status, "task_recovered")
            return len(rows)

    def resume(self, task_id: str, interrupt_id: str, value: object) -> None:
        """Persist human input before making work READY; repeat identical input safely."""
        with self.database.transaction(immediate=True) as session:
            self.resume_in_transaction(session, task_id, interrupt_id, value)

    def resume_in_transaction(
        self, session: Session, task_id: str, interrupt_id: str, value: object
    ) -> None:
        """Compose a resume with a durable inbox acknowledgment in one writer transaction."""
        row = self._get(session, task_id)
        stored = session.get(TaskMemory, task_id)
        memory = json.loads(stored.state_json) if stored else {}
        reply = {"interrupt_id": interrupt_id, "value": value}
        if memory.get("resume") == reply:
            return
        if row.task_status != "WAITING_HUMAN" or interrupt_id not in memory.get("interrupts", {}):
            raise ConcurrentUpdateError("Human interrupt is no longer pending")
        memory["resume"] = reply
        memory["continuation"] = True
        self._memory(session, row, memory)
        self._finish(session, row, "READY", "task_human_resumed")

    def abort(self, lease: Lease) -> None:
        with self.fence(lease) as session:
            row = self._get(session, lease.task_id)
            self._finish(session, row, "ABORTED", "task_aborted")

    def cancel(self, task_id: str) -> None:
        with self.database.transaction(immediate=True) as session:
            row = self._get(session, task_id)
            if row.task_status not in {"NEW", "READY", "WAITING_HUMAN"}:
                raise ConcurrentUpdateError("Task cannot be canceled in its current state")
            self._finish(session, row, "CANCELED", "task_canceled")
