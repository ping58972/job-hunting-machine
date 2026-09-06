# Database foundation — Phase 1

The database stores durable facts about work. A transaction groups related writes
so a failure cannot leave an Application without its details or first Resume task.
Repositories are the supported write interface; they record audit events and leave
the final commit to the caller's transaction.

## Ownership and initialization

The authoritative database is `data/job-hunting.db`. Only Alembic creates or changes
its schema. `Base.metadata` describes the ORM mappings; never call `create_all()`
against an application database.

```bash
cd /Users/ping58972/Documents/job-hunting-machine
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv run --offline --locked jhm db init
uv run --offline --locked alembic current
```

`jhm db init` applies migrations, then seeds policy in a separate transaction. It is
safe to repeat. A seed failure does not undo an already-applied schema migration;
repeat initialization after correcting the failure. No migration or seeding happens
on import, `jhm version`, or `jhm config`.

For an isolated contained database use `--database .tmp/example.db`. For direct
Alembic administration use `alembic -x db=.tmp/example.db upgrade head`; the `db`
argument is a guarded local filename, never an arbitrary SQLAlchemy connection URL.
Normal `Database.migrate()` refuses a nonempty database with no Alembic revision.

Revision `0001_architecture_v2` contains a frozen schema snapshot independent of
future ORM changes. Domain primary keys explicitly use `NOT NULL`: SQLite's bare
`TEXT PRIMARY KEY` otherwise permits null values, contrary to durable-ID intent.
The revision adds three append-only audit triggers and the two queue indexes specified
by Architecture v2. Future schema edits require new revisions, not edits to the
applied revision.

`data/langgraph-checkpoints.db` is owned by the Phase 2 LangGraph saver. The
application engine rejects that filename; application migrations never create its tables.

## Tables

All 23 domain tables specified in Architecture v2 are present, plus Alembic's
own `alembic_version` table:

| Area | Tables |
| --- | --- |
| Policy | `qualification_rule_sets`, `qualification_rules`, `salary_location_rules` |
| Jobs/applications | `check_job_position_quality`, `application_pipeline`, `application_details` |
| Durable work/audit | `agent_queue`, `task_memory`, `activity_log` |
| Slack records | `slack_events` |
| Candidate/source knowledge | `my_information_for_filling_form`, `candidate_facts`, `project_catalog`, `skill_catalog` |
| Application preparation | `artifacts`, `form_answers`, `browser_sessions`, `approvals` |
| External-action metadata | `external_actions`, `contacts`, `outreach_drafts`, `monitor_events`, `model_usage` |

Tables for later integrations are storage definitions only. Their agents, network
adapters, state machines, approval decisions, and schedulers have not been implemented.
Foreign keys, JSON checks, allowed-value checks, unique constraints, defaults, and
queue indexes follow the architecture. Optional unique keys retain SQLite's NULL
semantics: multiple rows may omit a dedupe key; non-null keys remain unique.

## Connections and file safety

Each native connection configures:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

The first four settings come from Architecture v2. In-memory temporary storage
prevents SQLite query workspaces from spilling outside the project root. NullPool
releases each connection at the end of its owner scope; every new connection applies
the settings again. Explicit `BEGIN` makes reads, savepoints, DML, and DDL participate
in transactions, avoiding sqlite3's legacy DDL autocommit behavior.

The engine reserves its database inode through `PathGuard.prepare_private_file()`
without truncation, makes it private, and validates the database and `-wal`, `-shm`,
and `-journal` files before native connections, statements, commits, and rollbacks.
Required SQLite settings cannot be disabled using SQL. The native authorizer denies
ATTACH and VACUUM INTO so SQL cannot redirect writes to another database.

SQLite performs native filesystem operations. These safeguards require trusted
project directories and do not replace a custom SQLite VFS or operating-system
sandbox. Raw third-party database connections bypass the configured engine and are
not supported application access. WAL with NORMAL synchronization follows the
architecture; it does not promise preservation of every recent commit through a
power failure. Committed data survives ordinary process close/reopen, as tested.

## Transactions and repositories

The supported repositories are:

| Repository | Operations |
| --- | --- |
| `JobRepository` | Create/read exact canonical URLs; store an explicit qualification status with audit |
| `TaskRepository` | Create/read durable tasks; dedupe replay; versioned status updates with audit |
| `ApplicationRepository` | Read applications/details; atomically create from a persisted PASSED job |
| `ActivityLogRepository` | Append events; read by Task/Application ID; no update/delete API |
| `ArtifactRepository` | Create/read metadata with ART IDs and a supplied SHA-256 digest |
| `ApprovalRepository` | Create/read PENDING metadata with APR IDs and exact payload metadata |

Repository constructors accept `(session, clock=None, ids=None)`. A supplied clock
and ID generator make tests deterministic. Repositories do not commit. Their write
operations use savepoints where several writes must succeed together, while the
outer transaction controls final persistence.

The following function takes an existing Job ID and supplied job details; it does
not evaluate qualifications or invent missing facts:

```python
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationCreation,
    ApplicationRepository,
)


def persist_application(job_id: str, details: ApplicationCreate) -> ApplicationCreation:
    database = Database()
    try:
        with database.transaction() as session:
            return ApplicationRepository(session).create_from_passed_job(job_id, details)
    finally:
        database.dispose()
```

The operation requires `qualification_status = PASSED`. It creates one
`application_pipeline` row, one `application_details` row, and one READY
`BUILD_RESUME` task, then appends audit events. The application is `QUALIFIED` at
pipeline stage `RESUME`; no Resume worker starts. The task has dedupe key
`build_resume:<application_id>`. An exception rolls back the operation's savepoint,
including audit events, even if its caller catches that exception. An outer
transaction failure also rolls back all previously successful operations in it.

On replay, identical creation input returns persisted IDs. Reusing a Task,
Application, Artifact, or Approval identity for conflicting input raises
`ReplayConflictError`. Canonical URL deduplication returns the original Job record;
it does not overwrite discovery metadata. URL normalization/fetching is deferred.

Task updates use compare-and-swap. Let `v` be the version the caller read. The
update matches both the Task ID and `version = v`, then writes `version = v + 1`.
For example, version 7 becomes 8; another caller still holding 7 receives
`ConcurrentUpdateError`. Its attempted change creates no audit event. This is a
storage primitive. Phase 2 callers use `QueueService` for policy, ownership checks,
and audited transitions; see [orchestration.md](orchestration.md).

## Durable IDs and timestamps

The centralized generator creates IDs only at persistence boundaries. Repositories
accept existing IDs and reuse them on retries. ORM ID types validate known prefixes
and canonical ULID representation on both writes and reads; they have no implicit
ID-generation defaults. Task/Application/Artifact/Approval IDs are respectively
`TASK_`, `APP_`, `ART_`, and `APR_` plus a 26-character ULID.

Operational timestamps are canonical UTC TEXT strings such as
`2026-09-05T08:23:31.123Z`. Repositories call `format_utc(clock.now())`; ORM timestamp
types reject noncanonical input. Posting dates and other date-only policy/job fields
remain plain text where the architecture specifies them. Raw SQL bypasses ORM
ID/timestamp validators, so supported writes go through typed repositories.

## Policy seed

The seed represents Architecture v2 section 11 as versioned, editable policy data:

- Internships intersect January–August 2027, are in the USA, are paid, are CPT-compatible,
  match the relevant technical fields, meet the location salary policy, and remain open.
- New-graduate/early-career timing is compatible with the architecture's May 2027
  graduation reference. Allowed countries follow the architecture's exact list.
- A normalized minimum of five or more professional years fails the experience rule.
  Ambiguous experience or work-authorization evidence requires review.
- U.S. full-time authorization distinguishes OPT compatibility, sponsorship support,
  explicit lack of support, and unknown evidence. Missing sponsorship language is
  never treated as proof of either support or lack of support.
- USA internship salary fallback is USD 20/hour. San Francisco and New York City
  overrides are USD 35/hour.

Seeding inserts missing versioned policy data, preserves edits/disabled rows, and
records a `POLICY_SEEDED` audit only when rows are inserted. Policy dates and work
authorization rules are not candidate facts. No candidate information is seeded,
and no qualification evaluator runs in this phase.

## Scope and validation

All tests run with socket network access blocked and use isolated project-local
databases. The Phase 1 report records acceptance totals and exact executed commands.
Phase 2 reuses the existing queue and memory schema and adds a separate LangGraph-owned
checkpoint database. No application schema migration was needed. There is no
business integration. Phase 3 adds ModelGateway and audited budget reservations in
the existing model_usage table. See [model-gateway.md](model-gateway.md). Phase 4 has not started.

## Architecture Amendment A1 artifact migration

Alembic revision `0002_latex_artifacts` adds `RESUME_TEX` and `COVER_LETTER_TEX`. The constraint
retains `RESUME_DOCX` and `COVER_LETTER_DOCX` so historical rows survive migration, while
`ArtifactRepository.create` rejects new legacy document artifacts. Current document workers create
same-version TEX/PDF pairs. Application detail pointers identify the active PDF; its TEX partner is
correlated by exact Application ID, Task ID, and artifact version.
