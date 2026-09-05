"""LangGraph-owned SQLite persistence, isolated from the application schema."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from job_hunting_machine.database.engine import _authorize_sql, _validate_database_files
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard, PathGuardError

CHECKPOINT_PATH = PROJECT_ROOT / "data/langgraph-checkpoints.db"


async def drain_on_cancel[T](operation: Awaitable[T]) -> T:
    """Cancellation cannot abandon an in-flight native SQLite commit or its fence."""
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep waiting even if a caller sends a second cancellation during shutdown.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()  # Retrieve any error while preserving the cancellation request.
        raise


async def run_blocking[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Drain synchronous database work before reporting task cancellation."""
    return await drain_on_cancel(asyncio.to_thread(function, *args, **kwargs))


class FencedSqliteSaver(AsyncSqliteSaver):
    """Only the current lease owner may commit a checkpoint or pending node write.

    The main database writer reservation spans the separate checkpoint commit.
    It is a fencing boundary, not a cross-database atomic transaction: restart
    reconciles a saved checkpoint whose queue status was not committed yet.
    """

    def __init__(
        self, connection: aiosqlite.Connection, path: Path, queue: QueueService, lease: Lease
    ) -> None:
        super().__init__(
            connection,
            serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None),
        )
        self.path = path
        self.queue = queue
        self.lease = lease
        self.write_lock = asyncio.Lock()

    async def _fenced[T](self, config: RunnableConfig, operation: Callable[[], Awaitable[T]]) -> T:
        if config.get("configurable", {}).get("thread_id") != self.lease.task_id:
            raise ValueError("Checkpoint thread must equal the persisted task ID")

        async def commit() -> T:
            async with self.write_lock:
                fence = self.queue.fence(self.lease)
                await asyncio.to_thread(fence.__enter__)
                try:
                    _validate_database_files(self.path)
                    return await operation()
                finally:
                    await asyncio.to_thread(fence.__exit__, None, None, None)

        return await drain_on_cancel(commit())

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        async def put() -> RunnableConfig:
            return await super(FencedSqliteSaver, self).aput(
                config, checkpoint, metadata, new_versions
            )

        return await self._fenced(config, put)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        async def put() -> None:
            await super(FencedSqliteSaver, self).aput_writes(config, writes, task_id, task_path)

        await self._fenced(config, put)

    async def adelete_thread(self, thread_id: str) -> None:
        raise RuntimeError("Checkpoint deletion is not exposed by the worker runtime")


@asynccontextmanager
async def checkpoint_connection(
    path: Path = CHECKPOINT_PATH,
) -> AsyncIterator[aiosqlite.Connection]:
    guard = PathGuard()
    target = guard.validate_write(path)
    if target.name != "langgraph-checkpoints.db":
        raise PathGuardError("Checkpoints require a separate langgraph-checkpoints.db")
    _validate_database_files(target)
    guard.mkdir(target.parent, parents=True, exist_ok=True)
    guard.prepare_private_file(target)
    async with aiosqlite.connect(f"{target.as_uri()}?mode=rw", uri=True, timeout=5) as connection:
        for pragma in (
            "busy_timeout=5000",
            "foreign_keys=ON",
            "synchronous=NORMAL",
            "temp_store=MEMORY",
        ):
            await connection.execute(f"PRAGMA {pragma}")
        saver = AsyncSqliteSaver(
            connection,
            serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None),
        )
        # Schema SQL belongs exclusively to the installed LangGraph saver.
        await drain_on_cancel(saver.setup())
        await connection.set_authorizer(_authorize_sql)
        try:
            yield connection
        finally:
            _validate_database_files(target)


@asynccontextmanager
async def open_checkpoints(
    queue: QueueService, lease: Lease, path: Path = CHECKPOINT_PATH
) -> AsyncIterator[FencedSqliteSaver]:
    async with checkpoint_connection(path) as connection:
        saver = FencedSqliteSaver(connection, path, queue, lease)
        saver.is_setup = True  # The library setup just ran on this connection.
        yield saver
