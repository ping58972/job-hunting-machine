"""Lease-aware async execution, durable interrupts and cooperative shutdown."""

import asyncio
import json
import signal
import sqlite3
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from langsmith.run_helpers import tracing_context
from sqlalchemy.exc import OperationalError

from job_hunting_machine.database.engine import DatabaseError
from job_hunting_machine.orchestration.checkpoints import (
    CHECKPOINT_PATH,
    checkpoint_connection,
    drain_on_cancel,
    open_checkpoints,
    run_blocking,
)
from job_hunting_machine.orchestration.queue import (
    Lease,
    LeaseLostError,
    QueueService,
    RetryableError,
)
from job_hunting_machine.orchestration.workflows import Workflow
from job_hunting_machine.runtime import RuntimeMode


class Worker:
    def __init__(
        self,
        queue: QueueService,
        workflows: Mapping[str, Workflow],
        *,
        checkpoint_path: Path = CHECKPOINT_PATH,
        heartbeat_seconds: float = 60,
        poll_seconds: float = 1,
        shutdown_seconds: float = 10,
    ) -> None:
        if not 0 < heartbeat_seconds < queue.lease_seconds:
            raise ValueError("Heartbeat interval must be positive and shorter than the lease")
        if poll_seconds <= 0 or shutdown_seconds < 0:
            raise ValueError("Invalid polling or shutdown interval")
        self.queue = queue
        self.runtime_mode = RuntimeMode.DRY_RUN
        self.workflows = dict(workflows)
        self.checkpoint_path = checkpoint_path
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_seconds = poll_seconds
        self.shutdown_seconds = shutdown_seconds
        self.stopping = asyncio.Event()

    def _check_schema(self) -> None:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).parents[1] / "database/migrations")
        )
        expected = set(ScriptDirectory.from_config(config).get_heads())
        with self.queue.database.engine.connect() as connection:
            actual = set(MigrationContext.configure(connection).get_current_heads())
        if actual != expected:
            raise DatabaseError("Run jhm db init before starting a worker")

    async def startup(self) -> int:
        await run_blocking(self._check_schema)
        async with checkpoint_connection(self.checkpoint_path):
            pass
        return await run_blocking(self.queue.recover)

    def request_stop(self) -> None:
        self.stopping.set()

    async def _execute(self, lease: Lease) -> None:
        workflow = self.workflows[lease.task_type]
        memory = await run_blocking(self.queue.memory, lease.task_id)
        if memory.get("workflow_version", workflow.version) != workflow.version:
            raise ValueError("Workflow version changed for an existing checkpoint thread")
        memory["workflow_version"] = workflow.version
        await run_blocking(self.queue.save_memory, lease, memory)
        # Disable ambient LangSmith tracing too. The Phase 2 runtime never makes network calls.
        with tracing_context(enabled=False):
            async with open_checkpoints(self.queue, lease, self.checkpoint_path) as saver:
                graph = workflow.graph.compile(checkpointer=saver)
                config: RunnableConfig = {
                    "configurable": {"thread_id": lease.task_id},
                    "callbacks": [],
                }
                snapshot = await graph.aget_state(config)
                interrupts = {
                    item.id: item.value for task in snapshot.tasks for item in task.interrupts
                }
                graph_input: Any = None
                if not snapshot.created_at:
                    graph_input = {"task_id": lease.task_id, "payload": lease.payload}
                elif interrupts:
                    reply = memory.get("resume")
                    if isinstance(reply, dict) and reply.get("interrupt_id") in interrupts:
                        graph_input = Command(resume={reply["interrupt_id"]: reply["value"]})
                    else:
                        await self._record(lease, memory, snapshot, interrupts)
                        return
                if not snapshot.created_at or snapshot.next:
                    await graph.ainvoke(graph_input, config, durability="sync")
                    snapshot = await graph.aget_state(config)
                interrupts = {
                    item.id: item.value for task in snapshot.tasks for item in task.interrupts
                }
                await self._record(lease, memory, snapshot, interrupts)

    async def _record(
        self, lease: Lease, memory: dict[str, object], snapshot: Any, interrupts: dict[str, Any]
    ) -> None:
        memory["checkpoint_id"] = snapshot.config.get("configurable", {}).get("checkpoint_id")
        memory["interrupts"] = interrupts
        memory["next"] = list(snapshot.next)
        # State remains in checkpoints; task_memory is a bounded working summary.
        await run_blocking(self.queue.complete, lease, memory, waiting=bool(interrupts))

    async def _heartbeat(self, lease: Lease) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await run_blocking(self.queue.heartbeat, lease)

    async def _attempt(self, lease: Lease) -> None:
        execution = asyncio.create_task(self._execute(lease))
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            done, _ = await asyncio.wait(
                (execution, heartbeat), return_when=asyncio.FIRST_COMPLETED
            )
            if execution in done:
                await execution
            else:
                await heartbeat
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        except LeaseLostError:
            pass  # New owner alone can publish results or record failure.
        except Exception as error:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            with suppress(LeaseLostError):
                await run_blocking(self.queue.fail, lease, retryable=is_retryable(error))
        finally:
            execution.cancel()
            heartbeat.cancel()
            await asyncio.gather(execution, heartbeat, return_exceptions=True)
            with suppress(LeaseLostError):
                await run_blocking(self.queue.release, lease)

    async def run_once(self) -> bool:
        if self.stopping.is_set():
            return False
        claiming = asyncio.create_task(asyncio.to_thread(self.queue.claim, tuple(self.workflows)))
        try:
            lease = await asyncio.shield(claiming)
        except asyncio.CancelledError:
            lease = await drain_on_cancel(claiming)
            if lease is not None:
                with suppress(LeaseLostError):
                    await drain_on_cancel(asyncio.to_thread(self.queue.release, lease))
            raise
        if lease is None:
            return False
        if self.stopping.is_set():
            with suppress(LeaseLostError):
                await drain_on_cancel(asyncio.to_thread(self.queue.release, lease))
            return False
        await self._attempt(lease)
        return True

    async def run(self, *, install_signals: bool = False) -> None:
        await self.startup()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.request_stop)
                installed.append(sig)
        try:
            while not self.stopping.is_set():
                await asyncio.to_thread(self.queue.recover)
                attempt = asyncio.create_task(self.run_once())
                stop = asyncio.create_task(self.stopping.wait())
                try:
                    done, _ = await asyncio.wait(
                        (attempt, stop), return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop in done:
                        try:
                            await asyncio.wait_for(asyncio.shield(attempt), self.shutdown_seconds)
                        except TimeoutError:
                            attempt.cancel()
                            await asyncio.gather(attempt, return_exceptions=True)
                    else:
                        await attempt
                finally:
                    stop.cancel()
                    attempt.cancel()
                    await asyncio.gather(attempt, stop, return_exceptions=True)
                if not self.stopping.is_set():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.stopping.wait(), self.poll_seconds)
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)


def inspect_memory(queue: QueueService, task_id: str) -> str:
    """Explicit local inspection; callers must not forward this content to logs."""
    return json.dumps(queue.memory(task_id), sort_keys=True, indent=2)


def is_retryable(error: Exception) -> bool:
    if isinstance(error, RetryableError):
        return True
    native = error.orig if isinstance(error, OperationalError) else error
    # Only lock contention is transient; invariant/schema errors remain fatal.
    return isinstance(native, sqlite3.OperationalError) and (
        getattr(native, "sqlite_errorcode", 0) & 0xFF
    ) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
