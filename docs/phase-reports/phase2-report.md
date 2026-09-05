# Phase 2 implementation report

Date: 2026-09-05

Status: **complete — stopped after Phase 2. Phase 3 has not started.**

## Scope and result

Read the repository AGENTS.md, authoritative Architecture v2, and Phase 0/1 reports.
Architecture v2 was preserved. Implemented the durable queue, leases, heartbeats,
recovery, retries, task memory, AsyncSqliteSaver checkpoints, human interrupt/resume,
and graceful worker shutdown. Only deterministic fixture workflows are registered.
No OpenAI calls, live Slack calls, real browser operations, or external business
integrations were implemented or used by the runtime/tests.

The default application database remains `data/job-hunting.db`. Startup initialized
`data/langgraph-checkpoints.db` through LangGraph's own saver. The checked startup
returned `runtime_mode=DRY_RUN`, `recovered=0`, `executed=false`; it created no tasks,
jobs, or applications.

## Files created

- `src/job_hunting_machine/orchestration/__init__.py`: public Phase 2 API.
- `src/job_hunting_machine/orchestration/queue.py`: atomic claims, ownership fences,
  heartbeats, audited transitions, retries, recovery, bounded memory, and human replies.
- `src/job_hunting_machine/orchestration/checkpoints.py`: guarded native SQLite
  setup, AsyncSqliteSaver write fencing, strict serialization, cancellation draining.
- `src/job_hunting_machine/orchestration/workflows.py`: versioned workflow contract
  and deterministic normal/human fixture graphs.
- `src/job_hunting_machine/orchestration/worker.py`: startup checks, graph resumption,
  heartbeat lifecycle, error classification, cooperative shutdown, and tracing disablement.
- `tests/integration/test_orchestration.py`: 26 queue/checkpoint/recovery acceptance tests.
- `docs/orchestration.md`: interfaces, replay rules, lifecycle, and storage limits.
- `docs/phase-reports/phase2-report.md`: this report.
- `data/langgraph-checkpoints.db`: ignored local runtime database, not a source artifact.

## Files updated

- `src/job_hunting_machine/database/engine.py`: explicit BEGIN IMMEDIATE transaction option.
- `src/job_hunting_machine/cli.py`: local fixture queue demo/inspect/resume and worker commands;
  Phase 2 capability reporting.
- `tests/unit/test_cli.py`: current CLI capability expectations.
- `pyproject.toml`, `uv.lock`: locked async SQLite/LangGraph dependencies and Phase 2 metadata.
- `AGENTS.md`, `README.md`, `docs/database.md`: current scope and execution contracts.

The architecture, previous reports, Phase 1 migration, candidate documents, and
qualification/salary seed policy were not changed.

## Architecture decisions

1. **Reuse the authoritative schema.** All 23 application domain tables remain under
   Alembic at `0001_architecture_v2`. Existing agent_queue and task_memory fields
   cover Phase 2; no application migration was necessary. No LangGraph tables were
   manually defined or added to the application database.
2. **Atomic ownership.** Claim reads and writes share a BEGIN IMMEDIATE transaction.
   Each claim receives a fresh centralized ULID ownership token. Every attempt-owned
   update validates the persisted token, attempt, status, and expiration. ORM versions
   and audit events commit with queue changes.
3. **Durable recovery.** Startup checks Alembic compatibility, opens the checkpoint
   database, requeues expired ACTIVE leases and due WAITING_RETRY rows, and preserves
   live leases and WAITING_HUMAN. Exhausted attempts become FAILED.
4. **Bounded retries.** Defaults are a 600-second lease, 60-second heartbeat, and
   30/120/600/1,800-second retry base delays with up to 20% injected jitter. Explicit
   transient errors and SQLite BUSY/LOCKED are retryable. Human/graceful continuations
   retain the attempt count and receive a fresh token; automatic retries increment it.
5. **Fenced checkpoint persistence.** AsyncSqliteSaver owns its separate schema.
   Both checkpoint and pending-node writes hold the application's ownership fence
   until the saver commit finishes. Cancellation drains commits before releasing the
   fence. This prevents stale writers without pretending the databases share a transaction.
6. **Stable replay identity.** LangGraph thread_id is exactly the persisted task_id.
   Workflows use synchronous checkpoint durability. Startup resumes pending work;
   completed checkpoints reconcile queue status without rerunning completed nodes.
   A persisted workflow version prevents accidental reuse with an incompatible graph.
7. **Durable human input.** Reply JSON and READY transition commit together before
   Command(resume=...) runs. Checkpoint inspection handles crashes before/after queue
   commits. Human workflow input does not implement external-action approval.
8. **Replay-sensitive logic stays deterministic.** Fixture nodes transform persisted
   input only. Clocks, retry randomness, domain IDs, database writes, and human input
   live at explicit persistence boundaries. LangGraph owns its internal checkpoint IDs.
9. **Separate execution and business state.** Failures change queue state and append
   audit history; they do not change application_pipeline or application_details.
10. **Local-only runtime.** The CLI registers FAKE/FAKE_HUMAN, never BUILD_RESUME.
    RuntimeMode remains DRY_RUN even with ambient LIVE configuration. Ambient LangSmith
    tracing is disabled; strict serialization disables pickle fallback.
11. **Guarded native storage.** Checkpoint paths and sidecars are validated, files
    reserved privately, and SQLite configured with WAL/FK/busy timeout/NORMAL/in-memory
    temporary storage. An authorizer prevents database attachment/export and disabling
    protected settings after library setup.

## Commands run

All setup/check commands ran from the fixed project root with project-local temporary
and uv cache directories. Commands below omit repeated focused debugging runs.

```bash
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv add 'langgraph>=1,<2' 'langgraph-checkpoint-sqlite>=3,<4' 'aiosqlite>=0.21,<1'
uv add --offline 'aiosqlite>=0.22,<1' 'langchain-core>=1.6.2,<2' 'langsmith>=0.12.1,<1'
uv run --offline --locked pytest tests/integration/test_orchestration.py -q -x
uv run --offline --locked ruff format src tests
uv run --offline --locked pytest -q
uv run --offline --locked ruff check .
uv run --offline --locked ruff format --check .
uv run --offline --locked mypy
uv run --offline --locked jhm worker --once
git diff --check
```

Dependency installation downloaded Python packages only. Subsequent checks and runtime
execution ran offline. Tests block outbound connections and DNS; the abrupt-death
subprocess also installs a socket guard. No external service credentials were needed.

Locked versions tested include Python 3.12, LangGraph 1.2.11, checkpoint-sqlite 3.1.1,
LangGraph checkpoint 4.2.0, aiosqlite 0.22.1, langchain-core 1.6.2, and langsmith 0.12.1.
LangSmith is present as a library dependency; tracing is explicitly disabled.

## Validation totals

- **pytest: 180 passed**, including all 154 prior-phase tests and 26 new Phase 2 tests.
- **Ruff: passed**.
- **Ruff formatting: passed**, 52 files checked.
- **Strict mypy: passed**, 45 source/test files checked.
- **git diff --check: passed**.
- **Default startup smoke: passed**, DRY_RUN, zero recovered/executed tasks.

## Acceptance scenarios

| Requested scenario | Evidence | Result |
| --- | --- | --- |
| 1. Worker claims one READY task | Concurrent two-thread claim test; one winner and one claim audit | PASS |
| 2. Second worker cannot claim the same task | Real SQLite writer transaction, unique live ownership | PASS |
| 3. Process death leaves recoverable task | Subprocess exits abruptly with os._exit(23) in second node; ACTIVE survives | PASS |
| 4. Lease expires | Injected UTC clock advances beyond 600 seconds; old owner is rejected | PASS |
| 5. New worker resumes | Reopened database and worker recover the expired attempt | PASS |
| 6. Completed node is not unnecessarily repeated | First node preserved across both retry and process death; completed graphs only reconcile | PASS |
| 7. WAITING_HUMAN survives restart | Persistent interrupts/replies, startup preservation, both queue/checkpoint commit windows | PASS |
| 8. Retryable error moves to WAITING_RETRY | Real failing graph and queue tests verify persisted delay | PASS |
| 9. max attempts produces FAILED | Always-failing graph reaches FAILED on attempt 2 of 2 | PASS |
| 10. Worker failure does not change app business state | Every pipeline/details column compared before and after failure | PASS |

Additional tests cover lease extension during a running async node, stale checkpoint
writes, ownership fencing during cancellation, graceful release, cancellation while
claiming, memory rollback/size limits, cancel/abort, workflow-version rejection,
unsafe checkpoint paths, schema mismatch, ambient tracing flags, SQLite busy
classification, unimplemented task types, and the complete local CLI human flow.

## Known limitations

- This phase has no real job-search nodes, ModelGateway, external action executor,
  approval decision service, scheduler cadence planner, browser, or communication integration.
- Nodes interrupted before their result commits can rerun. Arbitrary side effects
  are not exactly-once; future phases must use durable action/idempotency boundaries.
- SQLite is intended for modest local concurrency. Checkpoint fencing briefly blocks
  other application writers. Persistent database unavailability can stop a worker;
  its lease makes later recovery possible.
- Continuous workers handle SIGINT/SIGTERM cooperatively. Noncooperative synchronous
  Python/native work can outlast shutdown grace; this phase's fixtures are bounded.
- PathGuard protects the application boundary, not hostile concurrent filesystem
  namespace changes. Native SQLite requires trusted directories. Files are private,
  not encrypted, and checkpoint contents can include input and library error metadata.
- Tests demonstrate process death, not power-loss durability. synchronous=NORMAL may
  lose the newest commit on machine power failure. Coordinated backup/restore and
  workflow-version upgrades remain later-phase work.

Phase 2 is complete. No Phase 3 implementation was started.
