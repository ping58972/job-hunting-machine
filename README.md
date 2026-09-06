# Job Hunting Machine

Job Hunting Machine is a local-first workflow for collecting job links, qualifying jobs,
preparing evidence-backed resumes and application forms, requesting human approval, submitting
through a dedicated executor, drafting outreach, and monitoring application status.

The main idea is simple: Python handles deterministic work such as URL deduplication, salary
rules, dates, hashes, state transitions, and retries. AI is used only for bounded ambiguous work.
SQLite stores the authoritative state, and every important transition is auditable.

> **Current safety status:** the complete local and network-free test suite passes, but this
> repository is **not yet approved for LIVE use**. Real credentials, provider scopes, ATS markup,
> and a controlled real-world approval/reconciliation exercise have not been validated. The
> committed mode remains `DRY_RUN`. See the [Phase 12 report](docs/phase-reports/phase12-report.md).

## What the workflow does

```mermaid
flowchart TD
    A[Slack job URL] --> B[Retrieve and deduplicate link]
    B --> C[Fetch job evidence]
    C --> D{Deterministic qualification}
    D -->|Fail| E[Record evidence and abort]
    D -->|Unknown| F[Human review]
    D -->|Pass| G[Create Application and resume task]
    G --> H[Retrieve VERIFIED candidate facts]
    H --> I[Create resume and optional cover letter]
    I --> J[Prepare ATS form]
    J --> K[PREPARE_APPLICATION approval]
    K --> L[Fill form and stop at review]
    L --> M[Immutable review payload and hash]
    M --> N[SUBMIT_APPLICATION approval]
    N --> O[Dedicated Submission Agent]
    O --> P[Contact drafts and monitoring]
```

The Form Agent cannot submit. Slack buttons only record decisions. Final submission and email
sending are separate, approval-bound actions. CAPTCHA, MFA, missing information, uncertain
authorization, and unknown external results pause instead of being guessed or blindly retried.

## Requirements

- macOS or another local environment with Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/) installed
- the repository at the fixed Architecture v2 path:
  `/Users/ping58972/Documents/job-hunting-machine`
- Chromium only for browser staging or explicitly approved browser use
- provider accounts and credentials only for later, explicitly enabled integrations

The root path is a security boundary, not a convenience setting. A collaborator using another
computer must review and deliberately migrate that fixed-root design; changing it is an
architecture change.

## Setup

Run these commands from Terminal:

```bash
cd /Users/ping58972/Documents/job-hunting-machine
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"

python3 --version
uv --version
uv sync --locked
uv run --locked jhm version
uv run --locked jhm config
```

`uv sync --locked` creates the local `.venv` and installs the exact dependency versions from
`uv.lock`. Automatic Python downloads are disabled, so install Python 3.12 separately if the first
version check is too old.

Initialize or upgrade the authoritative database and seed the qualification policy:

```bash
uv run --locked jhm db init
uv run --locked jhm db integrity
uv run --locked jhm doctor
uv run --locked jhm status
```

The default database is `data/job-hunting.db`. Alembic owns its schema. The separate
`data/langgraph-checkpoints.db` is created when a durable graph worker first starts. Repeating
`jhm db init` is safe: it applies migrations and preserves existing application records and IDs.

`jhm doctor` may warn that the checkpoint database does not exist before the first worker run.
A warning does not fail the command. A failed invariant exits with status 2 and should be fixed
before starting workers.

Install Chromium only if you will run the local fake ATS or a separately reviewed browser flow.
Keep the browser binary under the project root:

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-binaries"
uv run --locked playwright install chromium
```

Use the same `PLAYWRIGHT_BROWSERS_PATH` value when starting a browser worker. This download is not
needed for database, queue, catalog, report, or other non-browser commands.

## First safe run

Use an isolated database to learn the queue and recovery flow without touching job data:

```bash
uv run --locked jhm db init --database .tmp/demo.db
uv run --locked jhm queue demo --database .tmp/demo.db
uv run --locked jhm worker --once --database .tmp/demo.db
uv run --locked jhm queue --database .tmp/demo.db
```

To test a durable human pause:

```bash
uv run --locked jhm queue demo --human --database .tmp/demo.db
uv run --locked jhm worker --once --database .tmp/demo.db
uv run --locked jhm queue --database .tmp/demo.db
uv run --locked jhm queue inspect TASK_ID --database .tmp/demo.db
uv run --locked jhm queue resume TASK_ID \
  --interrupt-id INTERRUPT_ID \
  --reply true \
  --database .tmp/demo.db
uv run --locked jhm worker --once --database .tmp/demo.db
```

Replace `TASK_ID` and `INTERRUPT_ID` with the displayed values. The reply is stored before the
task becomes eligible, and the worker resumes the same Task ID and LangGraph thread.

Browser tests can use the routed fake ATS with no external network request:

```bash
export JHM_RUNTIME_MODE=STAGING
uv run --locked jhm form worker --staging --once --database .tmp/form-demo.db
```

This command needs a prepared fixture application and `FORM_PROCESS` task in that database; the
automated test suite creates those fixtures. It never targets a real employer.

## Configuration

Configuration precedence is:

1. Pydantic defaults: `DRY_RUN` and `INFO`.
2. `config/runtime.yaml` and `config/logging.yaml`.
3. An optional root-local `.env` file.
4. Process environment variables.

Copy the example only when you need local overrides:

```bash
cp .env.example .env
```

Do not place API keys, OAuth tokens, passwords, browser cookies, or candidate secrets in `.env`,
YAML, Git, shell scripts, launchd files, Slack messages, or logs. Integration credentials are read
from the process environment and should come from secure storage with the smallest necessary
scope.

Runtime modes are:

| Mode | Meaning |
| --- | --- |
| `DRY_RUN` | Default. Inspect configuration and local state; external workflow mutations are disabled. |
| `STAGING` | Use supported local fake adapters for network-free workflow tests. |
| `LIVE` | Allows a command to check its additional live gates; it does not enable an integration by itself. |

Every integration has separate command and environment gates. For example, submission requires
configured `LIVE`, `--live`, `FORM_BROWSER_ALLOW_LIVE=1`, `SUBMISSION_ALLOW_LIVE=1`, and a current
hash-bound approval. Setting only `JHM_RUNTIME_MODE=LIVE` cannot submit or send anything.

## Starting an operator session

There is no single command that silently starts every integration. Start only the workers needed
for the current stage. At the beginning of a local session, use:

```bash
cd /Users/ping58972/Documents/job-hunting-machine
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"

uv run --locked jhm db init
uv run --locked jhm recover
uv run --locked jhm doctor
uv run --locked jhm status
uv run --locked jhm queue
```

Use one recovery coordinator. Recovery checks database compatibility, returns expired leases and
due retries to eligible states, preserves `WAITING_HUMAN`, and marks abandoned external effects
for explicit reconciliation. It never repeats an uncertain submission or email send.

## Using the job-processing workflow

The commands in this section describe the implemented workflow. Commands with real provider flags
must remain disabled until a separate LIVE readiness review has been completed.

### 1. Build the candidate knowledge base

Resume content can use only current `VERIFIED` facts with valid evidence. Start by requesting a
read-only incremental GitHub scan:

```bash
uv run --locked jhm catalog scan OWNER/REPOSITORY
uv run --locked jhm catalog inventory OWNER
```

These commands only enqueue tasks. The default worker only reports that GitHub access is disabled:

```bash
uv run --locked jhm catalog worker
```

When read-only GitHub access has been reviewed and explicitly enabled, process one queued scan:

```bash
export GITHUB_ALLOW_LIVE=1
export GITHUB_TOKEN='least-privilege-token-if-needed'
uv run --locked jhm catalog worker --live --once
```

Every extracted observation starts as `UNVERIFIED`. Inspect exact statements, provenance, and
hashes, then decide one fact at a time:

```bash
uv run --locked jhm catalog facts
uv run --locked jhm catalog decide FACT_ID VERIFIED \
  --value-sha256 VALUE_SHA256 \
  --reviewer YOUR_LOCAL_REVIEWER_NAME
uv run --locked jhm catalog retrieve "Python robotics machine learning"
```

Verification means the statement is truthful and appropriate for your application. Text appearing
in a repository is not by itself proof that you performed the work or achieved a metric.

### 2. Configure Slack intake and approvals

Edit `config/slack.yaml` with the exact Slack team, app, allowed channel, notification channel, and
authorized user IDs. Empty allowlists reject all inbound actions. Create a Slack app with Socket
Mode, interactivity, the required message/app-mention events, and only the scopes needed to post in
the selected channel.

Inspect the local configuration without connecting:

```bash
uv run --locked jhm slack
```

After a separate Slack readiness review, the explicitly gated listener is:

```bash
export SLACK_ALLOW_LIVE=1
export SLACK_BOT_TOKEN='xoxb-token-from-secure-storage'
export SLACK_APP_TOKEN='xapp-token-from-secure-storage'
uv run --locked jhm slack --live
```

Post a job URL in the configured Slack channel. Slack stores the event and creates a durable
`RETRIEVE_LINKS` task. Duplicate deliveries are deduplicated. Slack approval callbacks record a
decision and resume the exact task; they never execute submission or email sending.

### 3. Retrieve and qualify jobs

Qualification first applies deterministic country, date, salary, experience, paid-work, deadline,
and application-open rules. Missing CPT, sponsorship, or authorization evidence becomes review
instead of being invented.

The default command can process URL intake but performs no external job-page fetch:

```bash
uv run --locked jhm qualify --once
```

The real read-only fetch path has independent gates:

```bash
export JOB_FETCH_ALLOW_LIVE=1
export JOB_BROWSER_ALLOW_LIVE=1
uv run --locked jhm qualify --fetch-live --browser-live
```

`--browser-live` is optional and is used only as a read-only fallback after HTTP parsing. Optional
semantic ambiguity handling additionally requires ModelGateway:

```bash
export OPENAI_ALLOW_LIVE=1
export OPENAI_API_KEY='key-from-secure-storage'
uv run --locked jhm qualify --fetch-live --browser-live --models-live
```

A passed job atomically creates an Application, Application Details, and a `BUILD_RESUME` task.
A deterministic failure stores evidence and becomes `ABORTED`. Unknown evidence becomes
`NEEDS_REVIEW`. No qualification command submits an application.

### 4. Prepare the resume and optional cover letter

The configured source is `source/NDanddank_resume.gdoc`. It must be a valid local Google Docs
pointer for the intended template. The retained template content also needs an explicit verified
template fact. The complete procedure is in [Resume and cover-letter artifacts](docs/resume-artifacts.md).

The review sequence is:

```bash
export GOOGLE_DOCS_ALLOW_LIVE=1
export GOOGLE_ACCESS_TOKEN='short-lived-token-from-secure-storage'
uv run --locked jhm resume template-propose --live-docs --folder-id PRIVATE_FOLDER_ID
uv run --locked jhm catalog facts
uv run --locked jhm catalog decide TEMPLATE_FACT_ID VERIFIED \
  --value-sha256 VALUE_SHA256 \
  --reviewer YOUR_LOCAL_REVIEWER_NAME
```

After review, set `template_fact_id` in `config/resume.yaml`. Resume generation also uses
ModelGateway and therefore requires both explicit live flags:

```bash
export OPENAI_ALLOW_LIVE=1
export OPENAI_API_KEY='key-from-secure-storage'
uv run --locked jhm resume worker \
  --live-docs \
  --live-models \
  --folder-id PRIVATE_FOLDER_ID \
  --once
```

The worker copies the native template, changes only `PROJECTS` and `SKILLS`, uses verified facts,
exports a PDF, compresses content until it is exactly one page, records hashes, and creates
`FORM_PROCESS` only after artifact validation. Enable cover letters with the reviewed policy in
`config/resume.yaml`.

### 5. Prepare the application form

The Form Agent restores browser state, detects the ATS, resolves canonical fields, uploads the
exact application artifacts, and stops at `READY_TO_REVIEW`. Unknown fields, missing transcripts,
sensitive information, unsupported portals, CAPTCHA, MFA, and expired login state pause for a
human.

```bash
uv run --locked jhm form worker
```

The command above is an offline capability report. Real preparation requires configured `LIVE`,
`FORM_BROWSER_ALLOW_LIVE=1`, `--live`, and an approved, hash-bound `PREPARE_APPLICATION` plan:

```bash
uv run --locked jhm form worker --live --once
```

Do not run this real-site example under the current Phase 12 safety verdict. The Form Agent has no
final-submit method or action.

### 6. Review and submit

Generate immutable canonical review JSON and its SHA-256-bound Slack approval:

```bash
uv run --locked jhm submission review-worker --once
```

Review the exact form answers and attachment hashes, then approve or reject through the authorized
Slack interaction. A changed answer or artifact invalidates approval. The default submission
command is inert:

```bash
uv run --locked jhm submission worker
```

The dedicated live executor requires every independent gate:

```bash
export JHM_RUNTIME_MODE=LIVE
export FORM_BROWSER_ALLOW_LIVE=1
export SUBMISSION_ALLOW_LIVE=1
uv run --locked jhm submission worker --live --once
```

Do not run this command under the current safety verdict. A lost response after the click becomes
`UNKNOWN_RESULT`; recovery performs read-only reconciliation and never blindly clicks again.

### 7. Draft outreach

After confirmed submission, the Connector Agent can collect bounded public evidence and create
email or manual LinkedIn drafts. It never automates LinkedIn browsing or messaging.

```bash
uv run --locked jhm outreach connector
uv run --locked jhm outreach sender
```

These default commands are offline capability reports. Live contact discovery and Gmail draft
creation have separate read/write gates. Sending additionally requires an unchanged SEND_EMAIL
approval that binds recipient, subject, body, and attachment hashes. See
[Connector and outreach](docs/outreach.md) before configuring these integrations.

### 8. Monitor applications

Schedule due active applications locally:

```bash
uv run --locked jhm monitor schedule
uv run --locked jhm monitor worker
```

The default worker is an offline capability report. The live monitor uses bounded Gmail read-only
search and portal GET requests. It ignores unrelated or unchanged evidence, rejects low-confidence
destructive transitions, and stops scheduling terminal applications. See
[Application monitor](docs/application-monitor.md) for the explicit gates and Gmail scope.

## Daily operator commands

```bash
uv run --locked jhm status
uv run --locked jhm queue
uv run --locked jhm applications --limit 100
uv run --locked jhm costs
uv run --locked jhm db integrity
uv run --locked jhm doctor
uv run --locked jhm backup
```

| Command | Purpose |
| --- | --- |
| `jhm status` | Compact runtime, queue, approval, application, and unknown-action health. |
| `jhm queue` | Task counts by status/type, stale leases, human waits, and due retries. |
| `jhm applications` | Bounded application stage and status report. |
| `jhm costs` | Settled model cost and unresolved budget reservations. |
| `jhm db integrity` | SQLite, foreign keys, Alembic revision, WAL, and busy timeout. |
| `jhm doctor` | Database, artifacts, approvals, provenance, source boundaries, and evals. |
| `jhm backup` | SQLite-safe root-local backup with a hash manifest. |
| `jhm recover` | Conservative recovery after interruption. |

Backups are stored under `backups/` and remain Git-ignored. Architecture v2 recommends 30 daily
backups. Phase 12 does not automatically delete old backups. See
[Local operations and startup](docs/local-operations.md) for restoration, launchd examples,
shutdown, and incident handling.

## Important data locations

| Path | Contents |
| --- | --- |
| `data/job-hunting.db` | Authoritative application, queue, approval, action, usage, and audit state. |
| `data/langgraph-checkpoints.db` | Separate LangGraph checkpoints. |
| `data/browser-sessions/` | Private browser storage state; never commit or log it. |
| `evidence/` | Job, project, contact, and monitor evidence with hashes. |
| `applications/` | Immutable review payloads and application-local records. |
| `resumes/`, `cover-letters/` | Generated application artifacts. |
| `backups/` | SQLite-safe local backups and manifests. |
| `config/` | Runtime policies, model registry, prompts, Slack, resume, and monitor settings. |
| `docs/phase-reports/` | Implementation scope, tests, limitations, and safety results by phase. |

Generated data, credentials, candidate source documents, databases, logs, and browser sessions are
excluded from Git. Every workflow-generated write must remain under the project root through
`PathGuard` or an approved guarded integration boundary.

## Application and task states

Common task states are `READY`, `ACTIVE`, `WAITING_RETRY`, `WAITING_HUMAN`, `SUCCEEDED`, `FAILED`,
`ABORTED`, and `CANCELED`. Workers use leases and optimistic versions so two workers cannot own the
same task. A stale owner is fenced from committing after another worker recovers the lease.

Common application states include `FORM_READY`, `READY_TO_REVIEW`, `SUBMITTED`, `UNDER_REVIEW`,
`ASSESSMENT`, `RECRUITER_SCREEN`, `INTERVIEW`, `FINAL_INTERVIEW`, `OFFER`, `REJECTED`, `WITHDRAWN`,
and `CANCELED`. State changes must update the authoritative database and append audit evidence.

## Troubleshooting

### A task is stuck after a crash

```bash
uv run --locked jhm recover
uv run --locked jhm queue
uv run --locked jhm doctor
```

Wait for an active lease to expire. Do not edit lease or status columns manually.

### A task is waiting for a person

Inspect its memory and interrupt:

```bash
uv run --locked jhm queue inspect TASK_ID
```

Answer through the authorized Slack flow when available, or use `jhm queue resume` only when you
understand the exact interrupt contract. Never put secrets in shell history or Slack replies.

### The database is locked

SQLite uses WAL and a 5,000 ms busy timeout. Stop duplicate coordinators, allow the active writer
to finish, then inspect and recover. Do not copy an active database file with a normal filesystem
copy; use `jhm backup`.

### Submission or email result is unknown

Do not retry the external action manually. Preserve the database, checkpoint database, action
ledger, logs, and newest backup. The dedicated agent must reconcile provider evidence before a new
review or approval can be created.

### Browser login expired, CAPTCHA, or MFA appeared

The correct state is `WAITING_HUMAN`. Complete authentication or challenge handling manually, then
resume the exact task. The system never solves or bypasses CAPTCHA.

### Resume generation reports missing or unverified facts

Run `jhm catalog facts`, verify the source evidence and current commit, record a deliberate fact
decision, and retry through the existing task/recovery contract. Never add a fact merely to make a
resume pass validation.

## Development and validation

Run the complete network-free validation suite:

```bash
cd /Users/ping58972/Documents/job-hunting-machine
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"

uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
git diff --check
uv lock --check
```

The Phase 12 baseline is 402 passing tests, four evaluation datasets with 13 passing cases, Ruff
and strict mypy success, and no live external calls. Test fixtures use fake Slack, Gmail, GitHub,
Google Docs, model, and ATS adapters. The fault matrix covers process death, stale leases, SQLite
locking, model errors, duplicate deliveries and approvals, provider failures, browser failures,
missing/corrupted artifacts, submission ambiguity, duplicate jobs, deleted postings, and status
conflicts.

## Architecture and detailed guides

- [Architecture v2](docs/architecture-v2.md): authoritative design and invariants.
- [Architecture invariant review](docs/architecture-invariant-review.md): INV-001 through INV-018.
- [Local operations](docs/local-operations.md): startup, backup, recovery, launchd, and incidents.
- [Database](docs/database.md): schema, repositories, transactions, and policy seeds.
- [Durable orchestration](docs/orchestration.md): leases, checkpoints, retries, and interrupts.
- [ModelGateway](docs/model-gateway.md): model registry, budgets, prompts, and mock/live boundaries.
- [Slack control plane](docs/slack-control-plane.md): intake, allowlists, interactions, and delivery.
- [Qualification](docs/qualification.md): evidence, deterministic rules, semantic fallback, and limits.
- [Candidate knowledge](docs/candidate-knowledge.md): scanning, provenance, verification, and retrieval.
- [Resume artifacts](docs/resume-artifacts.md): template verification, generation, PDF QA, and recovery.
- [Form preparation](docs/form-preparation.md): ATS adapters, canonical fields, challenges, and approval.
- [Submission](docs/submission.md): immutable review, exact-content approval, idempotency, and reconciliation.
- [Outreach](docs/outreach.md): public contacts, drafts, email approval, and manual LinkedIn.
- [Application monitor](docs/application-monitor.md): scheduling, status evidence, confidence, and terminal states.
- [Phase 12 report](docs/phase-reports/phase12-report.md): final test totals, limitations, and LIVE verdict.

## Source layout

The Python package uses `src/job_hunting_machine`:

| Package | Responsibility |
| --- | --- |
| `database/` | SQLAlchemy models, Alembic, transactions, repositories, and policies. |
| `orchestration/` | Queue leases, retries, checkpoints, recovery, and human interrupts. |
| `agents/` | Link intake, job fetching, extraction, and qualification. |
| `knowledge/` | GitHub catalog, evidence, fact verification, and project retrieval. |
| `models/` | ModelGateway, OpenAI client boundary, routing, prompts, budgets, and usage. |
| `slack/` | Durable inbox/outbox, questions, notifications, and decision callbacks. |
| `resume/` | Verified-only resume and cover-letter planning and artifact validation. |
| `browser/` | Playwright lifecycle, ATS adapters, form resolution, and preparation actions. |
| `submission/` | Immutable review payloads, dedicated submission, and reconciliation. |
| `outreach/` | Contact evidence, drafts, Gmail actions, sender, and manual LinkedIn flow. |
| `monitor/` | Gmail/portal reads, classification, scheduling, transitions, and notifications. |
| `reliability/` | Backups, recovery, health checks, operational reports, and evals. |
| `security/`, `clock.py`, `ids.py` | Root confinement, centralized UTC time, and durable IDs. |

Architecture v2 describes the project as a reliable workflow system with AI components. It is not
a single autonomous agent, and it never treats model output as permission to perform an external
action.
