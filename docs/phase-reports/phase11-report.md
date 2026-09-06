# Phase 11 Implementation Report

## Scope

Phase 11 implements scheduled application monitoring through bounded read-only Gmail and portal
sources, deterministic application matching, Luna-only semantic classification through
ModelGateway, confidence and transition policy, root-confined evidence, durable Monitor Events,
and Slack notification only for a committed meaningful change. Phase 12 reliability work was not
started. Automated tests use fake Gmail, fake portal pages, fake Slack, and mock model responses.

## Files created

- `config/monitor.yaml`
- `src/job_hunting_machine/monitor/__init__.py`
- `src/job_hunting_machine/monitor/types.py`
- `src/job_hunting_machine/monitor/config.py`
- `src/job_hunting_machine/monitor/gmail.py`
- `src/job_hunting_machine/monitor/portal.py`
- `src/job_hunting_machine/monitor/classification.py`
- `src/job_hunting_machine/monitor/evidence.py`
- `src/job_hunting_machine/monitor/repository.py`
- `src/job_hunting_machine/monitor/service.py`
- `src/job_hunting_machine/monitor/scheduler.py`
- `src/job_hunting_machine/monitor/worker.py`
- `src/job_hunting_machine/monitor/cli.py`
- `tests/integration/test_monitor.py`
- `docs/application-monitor.md`
- `docs/phase-reports/phase11-report.md`

## Files updated

- `src/job_hunting_machine/submission/service.py`
- `src/job_hunting_machine/cli.py`
- `config/prompts.yaml`
- `tests/unit/test_cli.py`
- `AGENTS.md`
- `README.md`
- `.env.example`
- `pyproject.toml`

No Alembic migration was added. Architecture v2 revision `0001_architecture_v2` already owns the
`monitor_events`, application, task, model-usage, external-action, and audit tables.

## Architecture decisions

- Confirmed submission now creates independent, idempotent `CONNECT_CONTACTS` and
  `MONITOR_APPLICATION` tasks in the same successful submission transaction. Connector failure
  does not prevent monitoring and monitor retries do not alter Connector progress.
- `MonitorScheduler` selects only due active applications, suppresses applications with a pending
  monitor task, uses one check per day for `SUBMITTED` and `UNDER_REVIEW`, two per day for
  interview-stage states, and zero for terminal states. A per-run portal budget prevents bursts.
- Terminal states are the Architecture v2 set: `REJECTED`, `WITHDRAWN`, `CANCELED`, and `CLOSED`.
  The scheduler excludes them and the worker checks again before every portal read.
- Gmail monitoring has search/read methods only. It uses a bounded recent query, caps result and
  body sizes, accepts a short-lived process token, and has an independent
  `GMAIL_MONITOR_ALLOW_LIVE=1` gate. It has no draft, send, modify, or delete method.
- Gmail application matching is deterministic before classification. An exact Application ID or
  persisted company evidence is required; a job title alone cannot associate unrelated mail.
- Portal monitoring uses GET only, public HTTPS address validation, bounded content, and ordered
  adapters for Greenhouse, Lever, Ashby, Workday, SmartRecruiters, iCIMS, and Generic. It has no
  form fill or submit method and requires `PORTAL_MONITOR_ALLOW_LIVE=1`.
- Exact status phrases are classified deterministically. Ambiguous relevant evidence can use
  `LunaStatusClassifier`, which calls only ModelGateway's existing email or portal classification
  operation with an explicit Luna ceiling. Prompt content is versioned and model usage persists
  through ModelGateway.
- The ordinary transition threshold is 0.90. `REJECTED`, `WITHDRAWN`, and `CANCELED` require 0.95.
  The detected edge must also exist in the Architecture v2 application state machine. Low
  confidence, unchanged state, invalid edge, and terminal-state observations never mutate state.
- Email provider IDs are durable dedupe identities. Portal URL, HTTP status, and body form a
  SHA-256 observation identity. The immediate SQLite transaction repeats the dedupe check before
  insert, so concurrent/replayed handling records one Monitor Event.
- Relevant evidence is canonical JSON written through PathGuard beneath
  `evidence/monitor/<application-id>/`. Evidence files are private local workflow data and ignored
  by Git. The database stores their root-confined path.
- Every unique relevant observation gets a `monitor_events` row and append-only audit. A valid
  state transition updates both application status records and its audit atomically. Unchanged or
  rejected decisions remain visible with `meaningful_change=0`.
- Slack notification is planned only after at least one meaningful transition commits. It uses
  the existing closed `STATUS_CHANGED` template and Slack ExternalActionService. Duplicate or
  unchanged observations produce no notification.
- Explicitly transient Gmail/portal failures use QueueService's `WAITING_RETRY` path and configured
  backoff. Non-retryable failures fail the task without changing application business state.
- `jhm monitor worker` is inert by default. Staging uses fake readers. Live use requires configured
  `LIVE`, `--live`, both monitor transport flags, and ModelGateway's separate OpenAI live gate.

## Acceptance results

The Phase 11 integration suite verifies:

1. a rejection email updates only the correctly matched application;
2. an interview email advances an under-review application to `INTERVIEW`;
3. an unrelated company's email creates no Monitor Event and changes no state;
4. an unchanged portal observation is audited with `meaningful_change=0` and creates no Slack
   external action;
5. a portal rejection updates the application and details records to `REJECTED`;
6. `REJECTED`, `WITHDRAWN`, `CANCELED`, and `CLOSED` applications are never scheduled;
7. replaying one Gmail provider ID produces one Monitor Event;
8. a low-confidence model rejection is recorded but cannot make a destructive transition.

Additional assertions verify bounded active scheduling, durable retry state after transient Gmail
failure, evidence persistence, exact application/task correlation, synchronized status records,
and that semantic classification invokes `gpt-5.6-luna` with no escalation above Luna. Existing
Phase 0-10 tests remain green.

## Commands run

From the fixed project root with `TMPDIR="$PWD/.tmp"`:

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
git diff --check
uv lock --check
```

## Test totals

- Pytest: 385 passed, 0 failed.
- Phase 11 integration module: 14 passed, covering all required scenarios plus scheduler, retry,
  and Luna routing behavior.
- Ruff lint: passed.
- Ruff format check: passed for 166 files.
- mypy strict type checking: passed for 141 source files.
- Git whitespace check: passed.
- uv lock verification: passed with 87 resolved packages.

## Known limitations

- Gmail OAuth acquisition and refresh remain operator-managed. The live reader accepts a
  short-lived token and does not persist or refresh credentials.
- Gmail queries are application-specific and bounded to a recent window. Historical backfill,
  Gmail push notifications, and mailbox cursor persistence are not implemented.
- Portal adapters deliberately consume conservative text evidence. Authenticated portals,
  JavaScript-only status pages, MFA, CAPTCHA, and session recovery require human handling or a
  later read-only adapter; the monitor never bypasses them.
- Source deduplication uses the immediate serialized SQLite writer transaction because the
  authoritative Architecture v2 `monitor_events` table has no unique source-reference constraint.
- The state validator rejects status jumps missing from the Architecture v2 graph. For example, a
  direct `SUBMITTED` to `INTERVIEW` observation is retained as non-meaningful until another
  supported transition or human review establishes the intermediate state.
- Deterministic company matching is conservative and may ignore messages from third-party
  recruiting platforms without an exact company or Application ID. It does this rather than
  associating one email with the wrong application.
- Slack planning is durable, but actual live Socket Mode delivery remains the independently gated
  Phase 4 transport workflow.
- Phase 12 fault injection, long-running daemon supervision, operational metrics, backups, and
  production readiness evaluation are outside Phase 11.
