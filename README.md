# Job Hunting Machine

Phase 11 extends the local Python foundation for [Architecture v2](docs/architecture-v2.md).
It provides safe file writes, validated configuration, UTC clocks, ULID identifiers,
structured logging, an Alembic-managed SQLite database, audited repositories, and a local
administration CLI. Durable workers now run deterministic fixtures with leases,
LangGraph checkpoints, recovery, and human pause/resume. Workers retrieve links, qualify jobs,
maintain verified candidate knowledge, prepare native Google Docs resumes and cover letters, and
prepare approved ATS forms through `READY_TO_REVIEW`, create immutable review snapshots, and run
an approval-bound Submission Agent with conservative unknown-result reconciliation.
ModelGateway provides budgeted, structured OpenAI Responses
infrastructure with mock transport by default. Qualification uses deterministic policy first and optional budgeted semantic checks. Slack control now supports durable intake, questions,
notifications, and approval decisions, with fake transport by default.

Phase 11 adds rate-limited active-application scheduling, bounded read-only Gmail and portal
sources, deterministic application matching, Luna-only semantic status classification through
ModelGateway, root-local evidence, durable Monitor Events, strict state transitions, and Slack
notification only after a meaningful committed change. Terminal applications are excluded.

```bash
uv run --locked jhm monitor schedule
uv run --locked jhm monitor worker
```

The worker command reports offline capability by default. Network-free fixtures require
`--staging`. Live reads require configured `LIVE`, `--live`, `GMAIL_MONITOR_ALLOW_LIVE=1`,
`PORTAL_MONITOR_ALLOW_LIVE=1`, `OPENAI_ALLOW_LIVE=1`, and short-lived credentials. Gmail
monitoring exposes search/read only. See [application monitoring](docs/application-monitor.md).

Phase 8 form preparation remains documented in [browser and form preparation](docs/form-preparation.md).
The default form command is offline:

```bash
uv run --locked jhm form worker
```

It reports capability and performs no browser mutation. The network-free fake ATS path is explicit:

```bash
uv run --locked jhm form worker --staging --once --database .tmp/form-demo.db
```

The database must contain an eligible `FORM_PROCESS` task and validated application-specific resume.
Real-site preparation requires configured `LIVE`, `--live`, and `FORM_BROWSER_ALLOW_LIVE=1`.
The Form Agent still has no final-submit capability. Phase 9 creates a separate `CREATE_REVIEW`
task when preparation succeeds.

The review worker writes canonical JSON and creates a SHA-256-bound Slack approval:

```bash
uv run --locked jhm submission review-worker --once
```

The submission command is inert by default:

```bash
uv run --locked jhm submission worker
```

A real final click requires configured `LIVE`, `--live`, `SUBMISSION_ALLOW_LIVE=1`,
`FORM_BROWSER_ALLOW_LIVE=1`, an unexpired authorized-user approval, an unchanged review hash,
the correct application state, and an unused submission idempotency key. Slack callbacks only
record and resume the decision; the dedicated Submission Agent performs the separately gated action.

After confirmed submission, Phase 10 queues public company-contact discovery. The Connector Agent
captures source pages locally, deduplicates and ranks exact public contact details, creates email
and LinkedIn-manual drafts, and can create a Gmail draft. It has no email-send or LinkedIn browser
capability. The Outreach Sender is a separate LIVE-only worker:

```bash
uv run --locked jhm outreach connector
uv run --locked jhm outreach sender
```

These default commands are offline. Public reads require `CONTACT_DISCOVERY_ALLOW_LIVE=1`;
Gmail writes require `GMAIL_ALLOW_LIVE=1` and a short-lived access token. Sending additionally
requires configured `LIVE`, `--live`, `OUTREACH_SEND_ALLOW_LIVE=1`, and an unchanged SEND_EMAIL
approval covering recipient, subject, body, and attachment hashes. LinkedIn messages are drafts
for manual use only. See [connector and outreach operations](docs/outreach.md).

Phase 7 resume generation remains available. The default resume command is offline:

```bash
uv run --locked jhm resume worker --once
```

Actual generation requires explicit provider opt-ins, verified candidate facts, and a verified
template source. GDOC files are local pointers to copied native cloud documents, accompanied
by hashed document snapshots and validated one-page PDFs. Fonts and margins are never shrunk.

## Setup

Use Python 3.12 or newer and `uv`. The development interpreter is pinned to 3.12.
The project root is fixed by the architecture:

```bash
cd /Users/ping58972/Documents/job-hunting-machine
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv sync --locked
uv run --locked jhm version
uv run --locked jhm config
```

Install Python separately if 3.12 is unavailable; automatic Python downloads are
disabled. `uv` stores dependencies in `.venv` and its cache in `.uv-cache` inside
this repository. The temporary-directory setting keeps build temporary files local.
For a different installed supported interpreter, pass `--python /path/to/python` to
`uv sync`. Commit `uv.lock` so other installations use the tested dependency versions.

## Configuration

Configuration precedence, from lowest to highest, is:

1. Pydantic defaults: `DRY_RUN` and `INFO`.
2. `config/runtime.yaml` and `config/logging.yaml`.
3. The optional project-root `.env` file.
4. Process environment variables.

Only `JHM_RUNTIME_MODE` and `JHM_LOG_LEVEL` are supported. Unknown `JHM_` variables,
unknown YAML fields, invalid values, and unsafe file paths fail validation. Relative
configuration paths resolve against the project root, independent of the working
directory. No parent-directory dotenv search or variable interpolation occurs.
Copy `.env.example` to `.env` if local overrides are needed; keep secrets out of YAML
and Git. No credentials are needed for mock mode or normal tests.

Runtime modes are exactly `DRY_RUN`, `STAGING`, and `LIVE`. Log levels are `DEBUG`,
`INFO`, `WARNING`, `ERROR`, and `CRITICAL`. Reading a configured mode does not start
a runtime. The explicit `jhm worker` command runs only synthetic workflows in DRY_RUN,
even if configuration says LIVE. Slack connectivity requires separate explicit opt-in. Real-site
form preparation and final submission use independent explicit gates.
Contact discovery, Gmail drafts, and email sending have separate Phase 10 gates.
Gmail and portal monitoring have separate Phase 11 read-only gates.

```bash
uv run --locked jhm --help
uv run --locked jhm config --runtime-file config/runtime.yaml
uv run --locked python -m job_hunting_machine version
```

## Foundation interfaces

The package uses a `src` layout under `src/job_hunting_machine`:

| Module | Responsibility |
| --- | --- |
| `config.py`, `runtime.py` | Immutable Pydantic settings and runtime-mode enum |
| `security/paths.py` | Fixed project boundary and guarded local writes |
| `clock.py` | Injectable UTC clock and millisecond ISO-8601 formatting |
| `ids.py` | Central ULID and architecture-prefixed ID generation |
| `observability/logging.py` | Structured JSON logging to stderr |
| `cli.py` | Configuration/version inspection and explicit local database initialization |
| `database/` | SQLAlchemy models, Alembic migrations, transactions, repositories, and policy seeds |
| `orchestration/` | Audited queue service, lease-fenced checkpoints, worker recovery, and fixture graphs |
| `slack/` | Durable Slack inbox, safe outbox, approval decisions, and opt-in Socket Mode |
| `models/` | ModelGateway, registry, budgets, pricing, prompts, and Responses/mock transports |
| `browser/` | Playwright, ATS adapters, canonical fields, approvals, actions, and Form Agent |
| `submission/` | Immutable review payloads, final-action ledger, Submission Agent, and reconciliation |
| `outreach/` | Public contact evidence, ranked drafts, Gmail actions, and Outreach Sender |
| `monitor/` | Active scheduling, Gmail/portal reads, status classification, evidence, and transitions |

SQLAlchemy and Alembic implement the 23 Architecture v2 domain tables. The separate
Alembic version table tracks schema revision `0001_architecture_v2`.

## Local database setup

Initialize or upgrade the authoritative application database explicitly:

```bash
uv run --offline --locked jhm db init
```

This command applies Alembic migrations to `data/job-hunting.db` and seeds the
qualification/salary policy from Architecture v2. Repeating it preserves existing
records, policy edits, and IDs. It creates no jobs, applications, candidate facts,
external actions, or LangGraph checkpoint database. Policy dates are selection rules,
not verified candidate facts.

For an isolated database inside the project root:

```bash
uv run --offline --locked jhm db init --database .tmp/example.db
```

Direct schema administration also uses Alembic; it does not seed policy:

```bash
uv run --offline --locked alembic current
uv run --offline --locked alembic upgrade head
```

Every connection enables foreign keys, WAL, `synchronous=NORMAL`, and a 5,000 ms busy
timeout. SQL temporary storage stays in memory. Database files are private; paths
and journal sidecars pass PathGuard checks. SQL that attaches/exports another database
or disables required safety settings is blocked. SQLite performs native file IO, so
the database directory must remain trusted; see the limitations in the phase report.

Use `Database.transaction()` with repository methods. They flush and record audit
history but never commit independently. A transaction exception rolls everything back.
Application creation requires a persisted PASSED job and atomically creates its
pipeline row, details row, and one READY `BUILD_RESUME` task. Repeating the same
operation returns its existing IDs; conflicting replay data is rejected. Queue status
updates require an expected version and append an audit in the same transaction.

Activity history exposes only append/read operations, with migration-owned triggers
also blocking SQL UPDATE, DELETE, and replacement of existing events. Artifact and
Approval repositories store metadata;
new approvals remain PENDING until explicitly decided through ApprovalService.
Submission state changes and final actions are audited in the same durable database.

See [database interfaces and policy data](docs/database.md) for schema ownership,
transaction examples, durable ID rules, and Phase 1 boundaries.

## ModelGateway infrastructure

`config/models.yaml` defines models, routes, reasoning, and budget ceilings.
`config/prompts.yaml` contains semantic-versioned instructions. Inspect them with:

```bash
uv run --offline --locked jhm models
```

`ModelGateway` defaults to a scripted mock and requires a persisted task for each
request. Explicit live construction requires `OPENAI_ALLOW_LIVE=1` and an API key;
normal tests never make live calls. Qualification semantics use this gateway only when explicitly supplied.
See [model interfaces, pricing, and budget semantics](docs/model-gateway.md).

## Durable queue and checkpoints

Start the local worker after initializing the main database:

```bash
uv run --offline --locked jhm worker --once
```

Startup verifies the Alembic revision, initializes the library-owned
`data/langgraph-checkpoints.db`, recovers expired leases and due retries, and leaves
human waits unchanged. It does not claim `BUILD_RESUME` or other unimplemented task types.
Without `--once`, the worker polls until SIGINT/SIGTERM and drains checkpoint writes
before releasing its active lease.

For an explicit synthetic example, use an isolated project-local database:

```bash
uv run --offline --locked jhm db init --database .tmp/demo/jobs.db
uv run --offline --locked jhm queue demo --human --database .tmp/demo/jobs.db
uv run --offline --locked jhm worker --once --database .tmp/demo/jobs.db
uv run --offline --locked jhm queue inspect TASK_ID --database .tmp/demo/jobs.db
uv run --offline --locked jhm queue resume TASK_ID --interrupt-id INTERRUPT_ID --reply true --database .tmp/demo/jobs.db
uv run --offline --locked jhm worker --once --database .tmp/demo/jobs.db
```

Replace `TASK_ID` and `INTERRUPT_ID` with the returned identifiers. Human replies are
stored in the main database before the task becomes READY. The next worker resumes
the same LangGraph thread; its thread ID is exactly the persisted Task ID.
See [orchestration interfaces and recovery semantics](docs/orchestration.md).

### File writes

Every application file write must use `PathGuard`. Its boundary cannot be widened by
configuration. Tests may select a narrower directory inside the real project root.

```python
from job_hunting_machine.security.paths import PathGuard

guard = PathGuard()
guard.mkdir("reports", exist_ok=True)
guard.write_text("reports/example.txt", "Local artifact\n")
```

Parent traversal, outside absolute paths, and symbolic-link write paths are rejected.
The guarded write helpers use descriptor-relative, no-follow operations and atomic
replacement. New directories are private (`0700`); files are private (`0600`).
`validate_write()` is a preflight check, not a safe substitute for a guarded write:
do not validate a path and then write it with an unguarded third-party API.

This is an application safety boundary, not an operating-system sandbox. It cannot
constrain arbitrary Python, shell commands, or a hostile process moving already-open
directories outside the root. The project and its ancestors must remain trusted.
Later integrations must preserve this boundary when handing paths to external libraries.
The helpers fsync file contents, but do not fsync parent directory entries; atomic
replacement alone does not guarantee that a new filename survives a power failure.

### Time, IDs, and logs

Inject `Clock` into code that needs time. `SystemClock` supplies UTC time;
`FrozenClock` makes tests reproducible. `format_utc()` emits timestamps such as
`2026-09-05T08:23:31.123Z` and rejects naive datetimes.

`IdGenerator` creates ULIDs from a 48-bit millisecond timestamp and 80 bits of
cryptographic randomness. The 128-bit value is encoded as 26 Crockford Base32
characters. Task IDs match `^TASK_[0-9A-HJKMNP-TV-Z]{26}$`; Application IDs use
the same suffix with `APP_`. The other architecture prefixes are `JOB_`, `APR_`,
`ART_`, `CNT_`, and `EVT_`. Generate an ID once at its future persistence boundary;
do not regenerate it when replaying work. Phase 1 persists IDs and validates their
prefix/ULID representation at repository and ORM boundaries. Phase 2 reuses these
IDs across claims, retries, checkpoints, and restarts.

JSON logs include timestamp, level, Task ID, Application ID, agent, event, status,
duration, and error class. Use static event names and approved correlation fields.
Do not log candidate content, passwords, cookies, tokens, or arbitrary exception
messages. The logger filters fields; callers still must avoid placing sensitive
values in allowed fields. Persistent log files are deferred. Database state changes append separate `activity_log` events.

## Validation

From the project root with the setup above:

```bash
uv run --offline --locked pytest
uv run --offline --locked ruff check .
uv run --offline --locked ruff format --check .
uv run --offline --locked mypy
```

Pytest temporary files stay in `.pytest-tmp`; lint/type caches also stay in the
repository. Tests block outbound socket connections and DNS lookup, use synthetic
data, and exercise path confinement, configuration precedence, DRY_RUN defaults,
ID formats, deterministic clocks, structured logs, migrations, SQL constraints,
transaction rollback, optimistic concurrency, atomic application creation, seeding,
and persistence across restarts. Queue acceptance tests also terminate a subprocess
mid-workflow, resume saved nodes, fence stale writers, and preserve human interrupts.
No test uses an external service.

See [the Phase 11 implementation report](docs/phase-reports/phase11-report.md) for
the executed commands, acceptance results, and limitations. Existing candidate
documents remain untouched and ignored by Git. The [Phase 0 report](docs/phase-reports/phase0-report.md)
and [Phase 1 report](docs/phase-reports/phase1-report.md) are preserved as historical
evidence, together with the [Phase 2 report](docs/phase-reports/phase2-report.md).
The [Phase 3 report](docs/phase-reports/phase3-report.md) is also preserved.
The [Phase 4 report](docs/phase-reports/phase4-report.md) is preserved.
The [Phase 5 report](docs/phase-reports/phase5-report.md) is preserved.
The prior phase reports remain historical evidence. Phase 12 has not started.

## Slack control plane

Inspect the deny-by-default configuration without connecting:

```bash
uv run --offline --locked jhm slack
```

Live operation requires configured workspace, app, channel and user allowlists,
process environment credentials, `SLACK_ALLOW_LIVE=1`, and `jhm slack --live`.
Buttons record approval or rejection only; they never execute submission.
See [Slack setup, recovery, and security boundaries](docs/slack-control-plane.md).

## Link retrieval and qualification

```bash
uv run --offline --locked jhm qualify --once
```

The default command processes one URL intake task and leaves qualification tasks queued.
Public HTTP fetching requires `--fetch-live` and `JOB_FETCH_ALLOW_LIVE=1`.
Browser fallback additionally requires `--browser-live` and `JOB_BROWSER_ALLOW_LIVE=1`.
Semantic model calls require `--models-live` plus the existing OpenAI opt-in and credentials.
Python tests inject fake pages and mock model responses.

Passing jobs atomically create the Application, Details and a READY BUILD_RESUME task.
Failed jobs become ABORTED; unresolved jobs become NEEDS_REVIEW. See
[Phase 5 interfaces and limits](docs/qualification.md).

## Candidate knowledge and GitHub catalog

Phase 6 records source-backed project observations and candidate facts. GitHub scans are
incremental by commit and blob SHA. Extracted facts remain UNVERIFIED until explicit audited
review; resume retrieval exposes current VERIFIED facts only. Optional AI ranking can reorder
an already filtered shortlist and cannot introduce projects or claims.

```bash
uv run --offline --locked jhm catalog --help
uv run --offline --locked jhm catalog worker
uv run --offline --locked jhm catalog retrieve "Python robotics"
```

GitHub API reads require `--live` and `GITHUB_ALLOW_LIVE=1`. The default worker command does
not connect. See [candidate knowledge setup, verification, and limits](docs/candidate-knowledge.md).
The catalog remains read-only during later resume and submission phases.
