# Phase 2: durable execution

A queue row records **who may execute** a task. A LangGraph checkpoint records
**how far its workflow progressed**. The worker reconciles these records after a
restart. A worker failure changes queue execution state, never application business status.

## Ownership and transactions

`QueueService` is the execution API. `enqueue(TaskCreate(...))` inserts NEW,
transitions to READY, and records audit events atomically. Existing dedupe keys
return the original identity; conflicting replay data is rejected.

`claim(task_types)` uses `Database.transaction(immediate=True)`: SQLite reserves
its writer before reading the eligible row. Selection uses ascending priority,
creation time, then Task ID. READY tasks must be due, lack a live lease, and have a
registered task type. Exactly one row is claimed per call. Other workers cannot
acquire that same row. Exhausted READY rows become FAILED; the next poll can select another.

Every claim receives a fresh centralized ULID as `worker_id`. The ownership fence
checks Task ID, worker token, attempt count, ACTIVE status, and unexpired lease under
the same writer reservation. Heartbeats, results, errors, and task-memory updates
all pass this fence. ORM versions advance and audit events commit with the mutation.
The earlier `TaskRepository.update_status` remains a low-level persistence API;
it must not bypass queue transition policy.

| Setting | Default |
| --- | --- |
| Lease | 600 seconds |
| Heartbeat | 60 seconds |
| Idle polling | 1 second |
| Cooperative shutdown grace | 10 seconds |
| Automatic attempts | Task's `max_attempts`, default 3 |
| Retry base delays | 30, 120, 600, 1,800 seconds, capped at the last entry |

For base delay `b` and injected random fraction `u` in `[0, 1]`, the retry delay is
`b × (1 + 0.2 × u)`. For example, attempt 1 with `u = 0.5` waits
`30 × (1 + 0.1) = 33` seconds. The chosen UTC due timestamp is persisted once.
Randomness and clocks run at the queue boundary, outside graph logic.

The first claim and each automatic retry/recovery increment `attempt_count`.
An explicit human resume or graceful shutdown creates a durable continuation marker;
its next claim preserves the count while assigning a fresh ownership token. Normal
human waits and shutdowns therefore do not consume the failure budget. Abrupt
process death consumes an attempt; exhaustion produces FAILED.

## Checkpoints and replay

`data/job-hunting.db` remains Alembic-owned at `0001_architecture_v2`. No Phase 2
application migration is necessary: the architecture already includes lease,
version, retry, and memory fields.

`data/langgraph-checkpoints.db` is separate. `AsyncSqliteSaver` alone creates and
manages its schema. Application migrations never create its tables. A worker uses
`task_id` verbatim as `configurable.thread_id`, with synchronous checkpoint durability.
Checkpoint IDs and internal timestamps belong to LangGraph; domain IDs and operational
timestamps use the central `IdGenerator` and `Clock`.

`FencedSqliteSaver` wraps both checkpoint commits and pending-node writes. It holds
the main database writer reservation until the saver finishes its separate commit.
An expired worker cannot commit after another worker takes ownership. Cancellation
drains the write before releasing its fence. This is not a transaction across both
databases: recovery handles a checkpoint saved before the queue status update.

The worker inspects saved graph state before invocation:

- No checkpoint: use the original persisted payload and Task ID.
- Pending work/error: resume with `None`, preserving completed node results.
- Human interrupt: persist WAITING_HUMAN, or use its durable matching reply.
- Completed checkpoint: mark SUCCEEDED without rerunning graph nodes.

`task_memory` stores the workflow version, checkpoint reference, pending node names,
interrupts, and human reply. It is bounded to 64 KiB and versioned. Large sources
belong in artifacts referenced by path/hash. A workflow-version mismatch fails
closed; this phase has no graph-upgrade migration.

## Human input

`QueueService.resume(task_id, interrupt_id, value)` requires a current WAITING_HUMAN
interrupt. It persists JSON input and makes the task READY in one audited transaction.
Repeating the same input is idempotent; a stale or conflicting reply is rejected.
The worker uses `Command(resume={interrupt_id: value})`. If the process dies after
LangGraph accepts the reply but before the queue commits, the saved graph takes
precedence; the reply does not restart the graph from scratch.
Human input is a workflow primitive, not authorization for external actions;
ApprovalService remains unimplemented.

## Writing nodes

`Workflow(version, graph)` registers an explicit graph for a task type. The CLI
registers only FAKE and FAKE_HUMAN. It cannot run BUILD_RESUME or any real job-search
operation. Execution is DRY_RUN regardless of ambient mode variables. LangSmith
tracing is disabled around execution even when environment flags enable it.
Tests block outbound sockets and DNS.

Nodes must transform persisted inputs deterministically. Do not generate domain IDs,
read clocks, sample randomness, read mutable files, or perform external effects in
replay-sensitive node bodies. Obtain such values at explicit persistence boundaries
and pass references/results into state. Fake nodes only transform fixture strings.
Test-only counters, failure injection, and process termination instrument recovery;
they are not a production workflow pattern.

LangGraph reruns an interrupted node from its beginning on human resume. Put no
side effects before `interrupt()`. A node that died before a committed result may
also execute again. Completed durable nodes are reused; there is no blanket
exactly-once guarantee for arbitrary Python effects. Future effectful nodes must
use the architecture's durable action/idempotency services, not direct integrations.

## Startup, shutdown, and failure

`Worker.startup()` verifies the current Alembic revision, opens the saver database,
recovers stale ACTIVE leases, and promotes due WAITING_RETRY tasks. WAITING_HUMAN
and live leases remain unchanged. The continuous worker repeats recovery between
claims. `run_once()` is the lower-level one-claim primitive; an embedding caller
invokes `startup()` first. The CLI does this automatically.

`RetryableError` and SQLite BUSY/LOCKED errors are transient. Queue failure records
store static error codes, not exception messages. Fatal/invariant errors become
FAILED. Unknown errors fail closed. If the queue database itself cannot be written,
the error can stop the process; the durable lease permits later recovery. Neither
failure path updates application_pipeline or application_details.

For continuous workers, SIGINT/SIGTERM stops new claims. An active workflow gets the
grace interval, then cancellation drains checkpoint/database writes before returning
valid ownership to READY. Async nodes should cooperate with cancellation. A blocked
synchronous Python/native node can exceed the grace interval; leases and fencing
still protect a replacement process. Saver connections close when attempts exit;
the CLI disposes the application engine.

## Storage limits

Database files and sidecars pass PathGuard, are reserved privately, and use WAL,
foreign keys, a five-second busy timeout, synchronous NORMAL, and in-memory SQL
temporary storage. The authorizer rejects ATTACH/export, extension loading, and
changes to protected PRAGMAs after setup. Pickle fallback is disabled and checkpoint
deserialization uses strict allowed-type mode.

This is a local, modest-concurrency SQLite design. Checkpoint commits briefly
serialize application writers. It is not a distributed queue or custom SQLite VFS.
Trusted project directories and ancestors are required against concurrent filesystem
namespace attacks. Private SQLite files are not encrypted. Checkpoints may contain
workflow input, interrupt data, and library error metadata; keep them out of Git and
logs. Process-crash recovery is tested; synchronous NORMAL does not promise that the
latest commit survives machine power loss. Coordinated backup/restore of both
databases is deferred to its later architecture phase.
