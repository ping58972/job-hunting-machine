"""Phase 2 acceptance: real SQLite transactions and real LangGraph checkpoints."""

import asyncio
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import inspect, select

from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import ActivityLog
from job_hunting_machine.database.repositories import TaskCreate
from job_hunting_machine.orchestration import (
    LeaseLostError,
    QueueService,
    RetryableError,
    RetryPolicy,
    Worker,
    Workflow,
    fake_workflow,
)
from job_hunting_machine.orchestration.workflows import WorkflowState


@dataclass
class MutableClock:
    instant: datetime = datetime(2026, 9, 5, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


@pytest.fixture
def queue(tmp_path: Path) -> Iterator[QueueService]:
    database = Database(tmp_path / "jobs.db")
    database.migrate()
    yield QueueService(database, clock=MutableClock(), retry_policy=RetryPolicy(jitter=lambda: 0))
    database.dispose()


def advance(queue: QueueService, seconds: float) -> None:
    assert isinstance(queue.clock, MutableClock)
    queue.clock.advance(seconds)


def worker(queue: QueueService, workflow: Workflow | None = None, **kwargs: Any) -> Worker:
    return Worker(
        queue,
        {"FAKE": workflow or fake_workflow()},
        checkpoint_path=queue.database.path.parent / "langgraph-checkpoints.db",
        **kwargs,
    )


def test_atomic_concurrent_claims_and_audit(queue: QueueService) -> None:
    task_id = queue.enqueue(TaskCreate("FAKE", dedupe_key="one"))
    assert queue.enqueue(TaskCreate("FAKE", dedupe_key="one")) == task_id
    barrier = Barrier(2)

    def claim() -> object:
        barrier.wait()
        return queue.claim(["FAKE"])

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(value is not None for value in results) == 1
    row = queue.get(task_id)
    assert row.task_status == "ACTIVE"
    assert row.attempt_count == 1
    assert row.checkpoint_key == task_id
    with queue.database.transaction() as session:
        events = list(session.scalars(select(ActivityLog.event_type)))
    assert events.count("task_claimed") == 1


def test_expired_worker_is_fenced_and_new_owner_recovers(queue: QueueService) -> None:
    task_id = queue.enqueue(TaskCreate("FAKE"))
    lease = queue.claim(["FAKE"])
    assert lease
    assert queue.recover() == 0
    advance(queue, 601)
    operations: tuple[Callable[[], None], ...] = (
        lambda: queue.heartbeat(lease),
        lambda: queue.complete(lease, {}),
        lambda: queue.save_memory(lease, {}),
    )
    for operation in operations:
        with pytest.raises(LeaseLostError):
            operation()
    assert queue.recover() == 1
    new = queue.claim(["FAKE"])
    assert new and new.worker_id != lease.worker_id
    with pytest.raises(LeaseLostError):
        queue.fail(lease, retryable=False)
    queue.complete(new, {"summary": "done"})
    assert queue.get(task_id).task_status == "SUCCEEDED"


def test_heartbeat_extends_lease_and_versions(queue: QueueService) -> None:
    task_id = queue.enqueue(TaskCreate("FAKE"))
    lease = queue.claim(["FAKE"])
    assert lease
    first = queue.get(task_id)
    advance(queue, 300)
    queue.heartbeat(lease)
    after = queue.get(task_id)
    assert after.lease_expires_at is not None and first.lease_expires_at is not None
    assert after.lease_expires_at > first.lease_expires_at
    assert after.version > first.version
    advance(queue, 301)
    assert queue.recover() == 0


def test_retry_schedule_and_attempt_limit(queue: QueueService) -> None:
    task_id = queue.enqueue(TaskCreate("FAKE", max_attempts=2))
    lease = queue.claim(["FAKE"])
    assert lease
    queue.fail(lease, retryable=True)
    assert queue.get(task_id).task_status == "WAITING_RETRY"
    assert queue.recover() == 0
    advance(queue, 30)
    assert queue.recover() == 1
    lease = queue.claim(["FAKE"])
    assert lease
    queue.fail(lease, retryable=True)
    assert queue.get(task_id).task_status == "FAILED"
    assert queue.get(task_id).attempt_count == 2
    assert queue.claim(["FAKE"]) is None


def test_graph_completes_and_uses_separate_thread_database(queue: QueueService) -> None:
    task_id = queue.enqueue(TaskCreate("FAKE", payload={"text": "hello"}))
    assert asyncio.run(worker(queue).run_once())
    assert queue.get(task_id).task_status == "SUCCEEDED"
    assert queue.memory(task_id)["checkpoint_id"]
    assert "checkpoints" not in inspect(queue.database.engine).get_table_names()


def test_retry_resumes_without_repeating_completed_node(queue: QueueService) -> None:
    calls = {"first": 0, "second": 0}

    def first(state: WorkflowState) -> dict[str, str]:
        calls["first"] += 1  # Test instrumentation only; production nodes must be pure.
        return {"prepared": "durable"}

    def second(state: WorkflowState) -> dict[str, str]:
        calls["second"] += 1
        if calls["second"] == 1:
            raise RetryableError("synthetic failure")
        return {"result": state["prepared"]}

    graph = StateGraph(WorkflowState)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    workflow = Workflow("retry-v1", graph)
    task_id = queue.enqueue(TaskCreate("FAKE"))
    asyncio.run(worker(queue, workflow).run_once())
    assert queue.get(task_id).task_status == "WAITING_RETRY"
    advance(queue, 30)
    restarted = worker(queue, workflow)
    assert asyncio.run(restarted.startup()) == 1
    asyncio.run(restarted.run_once())
    assert queue.get(task_id).task_status == "SUCCEEDED"
    assert calls == {"first": 1, "second": 2}


def test_human_interrupt_and_reply_survive_restart(queue: QueueService) -> None:
    task_id = queue.enqueue(TaskCreate("FAKE", max_attempts=1))
    asyncio.run(worker(queue, fake_workflow(human=True)).run_once())
    assert queue.get(task_id).task_status == "WAITING_HUMAN"
    memory = queue.memory(task_id)
    assert isinstance(memory["interrupts"], dict)
    interrupt_id = next(iter(memory["interrupts"]))
    restarted = worker(queue, fake_workflow(human=True))
    assert asyncio.run(restarted.startup()) == 0
    assert not asyncio.run(restarted.run_once())
    queue.resume(task_id, interrupt_id, {"accepted": True})
    queue.resume(task_id, interrupt_id, {"accepted": True})
    asyncio.run(worker(queue, fake_workflow(human=True)).run_once())
    assert queue.get(task_id).task_status == "SUCCEEDED"


def test_shutdown_cancels_pending_node_and_releases_lease(queue: QueueService) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def blocked(state: WorkflowState) -> dict[str, str]:
            started.set()
            await asyncio.Event().wait()
            return {"result": "never"}

        graph = StateGraph(WorkflowState)
        graph.add_node("blocked", blocked)
        graph.add_edge(START, "blocked")
        graph.add_edge("blocked", END)
        task_id = queue.enqueue(TaskCreate("FAKE"))
        runner = worker(queue, Workflow("blocked-v1", graph), shutdown_seconds=0)
        run = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), 10)
        runner.request_stop()
        await asyncio.wait_for(run, 10)
        assert queue.get(task_id).task_status == "READY"
        assert queue.get(task_id).worker_id is None

    asyncio.run(scenario())


def test_actual_process_death_recovers_saved_node(queue: QueueService) -> None:
    import os
    import subprocess
    import sys

    from job_hunting_machine.security.paths import PROJECT_ROOT

    task_id = queue.enqueue(TaskCreate("FAKE"))
    script = """
import asyncio, os, socket, sys
from pathlib import Path
from datetime import UTC, datetime
from langgraph.graph import START, END, StateGraph
from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.orchestration import QueueService, Worker, Workflow
from job_hunting_machine.orchestration.workflows import WorkflowState

def blocked(*args, **kwargs):
    raise AssertionError("Network forbidden in subprocess")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked

def first(state):
    return {"prepared": "saved-before-death"}
def second(state):
    os._exit(23)

graph = StateGraph(WorkflowState)
graph.add_node("first", first)
graph.add_node("second", second)
graph.add_edge(START, "first")
graph.add_edge("first", "second")
graph.add_edge("second", END)
database = Database(Path(sys.argv[1]))
queue = QueueService(database, clock=FrozenClock(datetime(2026, 9, 5, tzinfo=UTC)))
worker = Worker(queue, {"FAKE": Workflow("death-v1", graph)},
                checkpoint_path=database.path.parent / "langgraph-checkpoints.db")
asyncio.run(worker.run_once())
"""
    environment = {**os.environ, "TMPDIR": str(PROJECT_ROOT / ".tmp")}
    result = subprocess.run(
        [sys.executable, "-c", script, str(queue.database.path)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 23, result.stderr.decode()
    assert queue.get(task_id).task_status == "ACTIVE"
    assert queue.recover() == 0
    advance(queue, 601)

    def first(state: WorkflowState) -> dict[str, str]:
        raise AssertionError("Completed node must not run again after process death")

    def second(state: WorkflowState) -> dict[str, str]:
        assert state["prepared"] == "saved-before-death"
        return {"result": state["prepared"]}

    graph = StateGraph(WorkflowState)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    # Reopen both database owners, not just an in-memory Worker object.
    restarted_db = Database(queue.database.path)
    restarted_queue = QueueService(restarted_db, clock=queue.clock)
    try:
        restarted = worker(restarted_queue, Workflow("death-v1", graph))
        assert asyncio.run(restarted.startup()) == 1
        assert asyncio.run(restarted.run_once())
        assert restarted_queue.get(task_id).task_status == "SUCCEEDED"
        assert restarted_queue.get(task_id).attempt_count == 2
    finally:
        restarted_db.dispose()


def test_completed_checkpoint_reconciles_queue_after_lost_commit(
    queue: QueueService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from job_hunting_machine.orchestration.checkpoints import open_checkpoints

    task_id = queue.enqueue(TaskCreate("FAKE"))
    lease = queue.claim(["FAKE"])
    assert lease

    async def save_graph_only() -> None:
        async with open_checkpoints(queue, lease, worker(queue).checkpoint_path) as saver:
            graph = fake_workflow().graph.compile(checkpointer=saver)
            await graph.ainvoke(
                {"task_id": task_id, "payload": {}},
                {"configurable": {"thread_id": task_id}},
                durability="sync",
            )

    asyncio.run(save_graph_only())
    # Simulates a crash after the final checkpoint commit, before queue completion.
    advance(queue, 601)
    queue.recover()
    graph = StateGraph(WorkflowState)

    def forbidden(state: WorkflowState) -> dict[str, str]:
        raise AssertionError("Completed graph should only reconcile the queue")

    graph.add_node("prepare", forbidden)
    graph.add_node("finish", forbidden)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "finish")
    graph.add_edge("finish", END)
    asyncio.run(worker(queue, Workflow("fake-v1", graph)).run_once())
    assert queue.get(task_id).task_status == "SUCCEEDED"


def test_checkpoint_writes_reject_stale_owner_and_wrong_thread(queue: QueueService) -> None:
    from job_hunting_machine.orchestration.checkpoints import open_checkpoints

    task_id = queue.enqueue(TaskCreate("FAKE"))
    lease = queue.claim(["FAKE"])
    assert lease

    async def scenario() -> None:
        async with open_checkpoints(queue, lease, worker(queue).checkpoint_path) as saver:
            with pytest.raises(ValueError, match="thread"):
                await saver.aput_writes({"configurable": {"thread_id": "other"}}, [], "node")
            advance(queue, 601)
            queue.recover()
            assert queue.claim(["FAKE"])
            with pytest.raises(LeaseLostError):
                await saver.aput_writes(
                    {
                        "configurable": {
                            "thread_id": task_id,
                            "checkpoint_id": "unused",
                            "checkpoint_ns": "",
                        }
                    },
                    [],
                    "node",
                )

    asyncio.run(scenario())


def test_task_failure_never_changes_application_business_state(queue: QueueService) -> None:
    from job_hunting_machine.database.models import ApplicationDetails, ApplicationPipeline
    from job_hunting_machine.database.repositories import (
        ApplicationCreate,
        ApplicationRepository,
        JobCreate,
        JobRepository,
    )

    with queue.database.transaction() as session:
        job = JobRepository(session, queue.clock).create(
            JobCreate(
                original_url="https://example.invalid/phase2",
                canonical_url="https://example.invalid/phase2",
                source_type="TEST",
                company_name="Synthetic",
                job_title="Synthetic",
                qualification_status="PASSED",
            )
        )
        result = ApplicationRepository(session, queue.clock).create_from_passed_job(
            job.job_id, ApplicationCreate("Synthetic", "Synthetic", job.canonical_url)
        )
        application_id = result.application_id
        before_pipeline = session.get(ApplicationPipeline, application_id)
        before_details = session.get(ApplicationDetails, application_id)
        assert before_pipeline and before_details
        pipeline_values = {
            c.name: getattr(before_pipeline, c.name) for c in before_pipeline.__table__.columns
        }
        detail_values = {
            c.name: getattr(before_details, c.name) for c in before_details.__table__.columns
        }
    lease = queue.claim(["BUILD_RESUME"])
    assert lease
    queue.fail(lease, retryable=False)
    with queue.database.transaction() as session:
        after_pipeline = session.get(ApplicationPipeline, application_id)
        after_details = session.get(ApplicationDetails, application_id)
        assert after_pipeline and after_details
        assert {
            c.name: getattr(after_pipeline, c.name) for c in after_pipeline.__table__.columns
        } == pipeline_values
        assert {
            c.name: getattr(after_details, c.name) for c in after_details.__table__.columns
        } == detail_values


def test_ambient_tracing_does_not_enable_network(
    queue: QueueService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    task_id = queue.enqueue(TaskCreate("FAKE"))
    asyncio.run(worker(queue).run_once())
    assert queue.get(task_id).task_status == "SUCCEEDED"


def test_wrong_workflow_version_fails_without_replaying(queue: QueueService) -> None:
    task_id = queue.enqueue(TaskCreate("FAKE"))
    lease = queue.claim(["FAKE"])
    assert lease
    queue.save_memory(lease, {"workflow_version": "old-version"})
    queue.release(lease)
    asyncio.run(worker(queue).run_once())
    assert queue.get(task_id).task_status == "FAILED"


def test_memory_size_and_lease_failures_roll_back(queue: QueueService) -> None:
    task_id = queue.enqueue(TaskCreate("FAKE"))
    lease = queue.claim(["FAKE"])
    assert lease
    before = queue.get(task_id).version
    with pytest.raises(ValueError, match="concise"):
        queue.complete(lease, {"large": "x" * 65536})
    assert queue.get(task_id).version == before
    assert queue.get(task_id).task_status == "ACTIVE"
    assert queue.memory(task_id) == {}


def test_cancel_and_abort_are_audited(queue: QueueService) -> None:
    first = queue.enqueue(TaskCreate("FAKE"))
    queue.cancel(first)
    assert queue.get(first).task_status == "CANCELED"
    second = queue.enqueue(TaskCreate("FAKE"))
    lease = queue.claim(["FAKE"])
    assert lease
    queue.abort(lease)
    assert queue.get(second).task_status == "ABORTED"


def test_checkpoint_paths_reject_symlink_and_application_database(
    queue: QueueService, tmp_path: Path
) -> None:
    from job_hunting_machine.orchestration.checkpoints import checkpoint_connection
    from job_hunting_machine.security.paths import PathGuardError

    async def open_path(path: Path) -> None:
        async with checkpoint_connection(path):
            pass

    with pytest.raises(PathGuardError):
        asyncio.run(open_path(queue.database.path))
    checkpoint = tmp_path / "langgraph-checkpoints.db"
    checkpoint.symlink_to(queue.database.path)
    with pytest.raises(PathGuardError):
        asyncio.run(open_path(checkpoint))


def test_schema_must_be_current_before_startup(tmp_path: Path) -> None:
    from job_hunting_machine.database.engine import DatabaseError

    database = Database(tmp_path / "empty.db")
    try:
        with pytest.raises(DatabaseError, match="db init"):
            asyncio.run(worker(QueueService(database)).startup())
    finally:
        database.dispose()


def test_human_checkpoint_recovers_both_queue_commit_windows(
    queue: QueueService, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = queue.enqueue(TaskCreate("FAKE", max_attempts=5))
    workflow = fake_workflow(human=True)
    original = Worker._record

    async def lost_commit(*args: Any, **kwargs: Any) -> None:
        advance(queue, 601)
        raise LeaseLostError("Simulated process stopped before queue commit")

    monkeypatch.setattr(Worker, "_record", lost_commit)
    asyncio.run(worker(queue, workflow).run_once())
    assert queue.get(task_id).task_status == "ACTIVE"
    advance(queue, 601)
    queue.recover()
    monkeypatch.setattr(Worker, "_record", original)
    asyncio.run(worker(queue, workflow).run_once())
    assert queue.get(task_id).task_status == "WAITING_HUMAN"
    interrupts = queue.memory(task_id)["interrupts"]
    assert isinstance(interrupts, dict)
    queue.resume(task_id, next(iter(interrupts)), True)
    monkeypatch.setattr(Worker, "_record", lost_commit)
    asyncio.run(worker(queue, workflow).run_once())
    assert queue.get(task_id).task_status == "ACTIVE"
    advance(queue, 601)
    queue.recover()
    monkeypatch.setattr(Worker, "_record", original)
    asyncio.run(worker(queue, workflow).run_once())
    assert queue.get(task_id).task_status == "SUCCEEDED"


def test_checkpoint_commit_fence_blocks_ownership_transfer(queue: QueueService) -> None:
    from job_hunting_machine.orchestration.checkpoints import open_checkpoints

    task_id = queue.enqueue(TaskCreate("FAKE"))
    lease = queue.claim(["FAKE"])
    assert lease

    async def scenario() -> None:
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def native_commit() -> None:
            entered.set()
            await finish.wait()

        async with open_checkpoints(queue, lease, worker(queue).checkpoint_path) as saver:
            write = asyncio.create_task(
                saver._fenced({"configurable": {"thread_id": task_id}}, native_commit)
            )
            await asyncio.wait_for(entered.wait(), 5)
            advance(queue, 601)
            recovery = asyncio.create_task(asyncio.to_thread(queue.recover))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(recovery), 0.05)
            # Canceling the caller must keep the fence until the native commit drains.
            write.cancel()
            await asyncio.sleep(0.01)
            assert not write.done()
            assert not recovery.done()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await write
            assert await asyncio.wait_for(recovery, 5) == 1

    asyncio.run(scenario())
    assert queue.get(task_id).task_status == "READY"


def test_worker_heartbeats_during_async_node(
    queue: QueueService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        heartbeated = asyncio.Event()
        original = queue.heartbeat
        loop = asyncio.get_running_loop()

        def heartbeat(lease: Any) -> None:
            original(lease)
            loop.call_soon_threadsafe(heartbeated.set)

        monkeypatch.setattr(queue, "heartbeat", heartbeat)

        async def pending(state: WorkflowState) -> dict[str, str]:
            started.set()
            await asyncio.Event().wait()
            return {}

        graph = StateGraph(WorkflowState)
        graph.add_node("pending", pending)
        graph.add_edge(START, "pending")
        graph.add_edge("pending", END)
        task_id = queue.enqueue(TaskCreate("FAKE", max_attempts=1))
        runner = worker(
            queue, Workflow("heartbeat-v1", graph), heartbeat_seconds=0.05, shutdown_seconds=0
        )
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), 5)
        advance(queue, 300)
        await asyncio.wait_for(heartbeated.wait(), 5)
        advance(queue, 301)
        assert queue.recover() == 0
        runner.request_stop()
        await asyncio.wait_for(task, 5)
        assert queue.get(task_id).task_status == "READY"
        lease = queue.claim(["FAKE"])
        assert lease  # Graceful continuation does not consume another failed attempt.
        assert lease.attempt == 1

    asyncio.run(scenario())


def test_cancel_during_claim_releases_result(
    queue: QueueService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    original = queue.claim
    started, finish = Event(), Event()

    def slow_claim(types: Any) -> Any:
        result = original(types)
        started.set()
        assert finish.wait(5)
        return result

    monkeypatch.setattr(queue, "claim", slow_claim)
    task_id = queue.enqueue(TaskCreate("FAKE"))

    async def scenario() -> None:
        runner = asyncio.create_task(worker(queue).run_once())
        assert await asyncio.to_thread(started.wait, 5)
        runner.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await runner

    asyncio.run(scenario())
    assert queue.get(task_id).task_status == "READY"
    assert queue.get(task_id).worker_id is None


def test_queue_cli_fixture_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from typer.testing import CliRunner

    from job_hunting_machine.cli import app

    runner = CliRunner()
    database = str(tmp_path / "cli.db")
    assert runner.invoke(app, ["db", "init", "--database", database]).exit_code == 0
    created = runner.invoke(app, ["queue", "demo", "--human", "--database", database])
    assert created.exit_code == 0, created.output
    task_id = created.stdout.strip()
    monkeypatch.setenv("JHM_RUNTIME_MODE", "LIVE")
    run = runner.invoke(app, ["worker", "--once", "--database", database])
    assert run.exit_code == 0, run.output
    assert json.loads(run.stdout)["runtime_mode"] == "DRY_RUN"
    inspected = runner.invoke(app, ["queue", "inspect", task_id, "--database", database])
    state = json.loads(inspected.stdout)
    assert state["status"] == "WAITING_HUMAN"
    interrupt_id = next(iter(state["memory"]["interrupts"]))
    resumed = runner.invoke(
        app,
        [
            "queue",
            "resume",
            task_id,
            "--interrupt-id",
            interrupt_id,
            "--reply",
            "true",
            "--database",
            database,
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    assert runner.invoke(app, ["worker", "--once", "--database", database]).exit_code == 0
    inspected = runner.invoke(app, ["queue", "inspect", task_id, "--database", database])
    assert json.loads(inspected.stdout)["status"] == "SUCCEEDED"


def test_worker_retry_budget_becomes_failed(queue: QueueService) -> None:
    def fail(state: WorkflowState) -> dict[str, str]:
        raise RetryableError("synthetic private exception text")

    graph = StateGraph(WorkflowState)
    graph.add_node("fail", fail)
    graph.add_edge(START, "fail")
    graph.add_edge("fail", END)
    task_id = queue.enqueue(TaskCreate("FAKE", max_attempts=2))
    workflow = Workflow("always-fails-v1", graph)
    asyncio.run(worker(queue, workflow).run_once())
    assert queue.get(task_id).task_status == "WAITING_RETRY"
    advance(queue, 30)
    queue.recover()
    asyncio.run(worker(queue, workflow).run_once())
    row = queue.get(task_id)
    assert row.task_status == "FAILED"
    assert row.attempt_count == 2
    assert row.last_error_message is None
    assert row.last_error_code == "RETRYABLE_ERROR"


def test_database_busy_classification() -> None:
    import sqlite3

    from job_hunting_machine.orchestration.worker import is_retryable

    busy = sqlite3.OperationalError("synthetic database busy")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT
    assert is_retryable(busy)
    assert not is_retryable(sqlite3.OperationalError("missing table"))
    assert not is_retryable(ValueError("invariant"))


def test_worker_ignores_unimplemented_task_types(queue: QueueService) -> None:
    task_id = queue.enqueue(TaskCreate("BUILD_RESUME"))
    assert not asyncio.run(worker(queue).run_once())
    assert queue.get(task_id).task_status == "READY"
