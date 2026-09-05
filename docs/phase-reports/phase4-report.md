# Phase 4 report: Slack Control Plane

Completed 2026-09-05. Architecture v2 remains authoritative. Phase 5 was not started.

## Delivered

Slack Bolt + Socket Mode infrastructure now supports durable inbound events,
deduplication, bounded URL intake queue events, task notifications, missing-information
questions, and interactive approval/rejection. Default transport is fake and network-free.
Live operation requires explicit configuration, process credentials, an environment opt-in,
and the CLI `--live` flag. No live Slack or OpenAI calls were made during validation.

Buttons record audited approval decisions only. They neither submit applications nor
create submission tasks/actions, and do not change application business state.

## Files created

- `config/slack.yaml`
- `src/job_hunting_machine/slack/__init__.py`
- `src/job_hunting_machine/slack/config.py`
- `src/job_hunting_machine/slack/messages.py`
- `src/job_hunting_machine/slack/adapter.py`
- `src/job_hunting_machine/slack/actions.py`
- `src/job_hunting_machine/slack/approvals.py`
- `src/job_hunting_machine/slack/inbound.py`
- `src/job_hunting_machine/slack/control.py`
- `src/job_hunting_machine/slack/socket_mode.py`
- `tests/integration/test_slack_control.py`
- `docs/slack-control-plane.md`
- `docs/phase-reports/phase4-report.md`

Updated `pyproject.toml`, `uv.lock`, `AGENTS.md`, `README.md`, `.env.example`,
`src/job_hunting_machine/cli.py`, `tests/unit/test_cli.py`, and
`src/job_hunting_machine/orchestration/queue.py`.

## Architecture decisions

1. Reuse the existing Alembic-managed Slack event, queue, approval, external-action,
   memory and activity tables. No schema migration was necessary; no production schema
   was manually modified and no LangGraph tables were introduced.
2. Persist sanitized input and its processing task atomically before Bolt acknowledges
   delivery. Event effects and processing status commit together under a worker lease.
   Replaying after a crash skips effects already committed.
3. Correlate interactions with a successful outbound receipt, exact channel/message,
   Task/Application IDs, approval ID and review hash. Check authorization again at processing
   time, including allowlist changes after receipt.
4. ApprovalService checks expiry, payload hash and relevant application state, and only
   records a decision. ExternalActionService owns all Slack sends. Uncertain expired sends
   become UNKNOWN_RESULT and are not automatically repeated.
5. Add QueueService.resume_in_transaction so human replies, continuation state and inbox
   completion share one transaction. Existing checkpoint/worker interfaces remain intact.
6. Send only closed templates with fixed text, IDs and hashes. Do not echo inbound text,
   answers, review content, paths, secrets, or exception details. Suppress SDK diagnostics.
7. Keep configuration deny-by-default and fake transport the default. Pin resolved
   slack-bolt 1.30.0 and slack-sdk 3.44.1 through uv.lock.

## Commands and results

Commands ran from the project root with `.tmp` present and `TMPDIR="$PWD/.tmp"`:

```bash
uv add 'slack-bolt>=1.20,<2' 'slack-sdk>=3.33,<4'
uv run --offline --locked pytest tests/integration/test_slack_control.py
uv run --offline --locked ruff format .
uv run --offline --locked pytest
uv run --offline --locked ruff check .
uv run --offline --locked ruff format --check .
uv run --offline --locked mypy
uv run --offline --locked jhm slack
uv run --offline --locked jhm config
git diff --check
```

- Full pytest: **249 passed** in 7.90 seconds; no failures or skips.
- Slack integration suite: **28 passed**; one additional offline CLI test.
- Ruff check: passed. Initial long lines were corrected before the final clean run.
- Ruff format check: **75 files already formatted**.
- Strict mypy: passed, **64 source files**.
- CLI inspection: empty allowlists and DRY_RUN confirmed without connecting.
- Diff whitespace check: passed.

## Acceptance results

| Requirement | Evidence | Result |
| --- | --- | --- |
| Duplicate event processed once | Repeated and concurrent deliveries create one inbox/task and one intake effect | PASS |
| Unauthorized user cannot approve | Ingress rejection and processing-time allowlist recheck | PASS |
| Correct application/task mapping | Receipt binding, wrong-message rejection and unchanged business state | PASS |
| Restart preserves pending approval | New service instance processes saved approval/callback | PASS |
| Malformed interaction rejected | Invalid structures/actions/values rejected without effects | PASS |
| Fake adapter without network | Fake sends, real Bolt offline dispatch and patched SDK request construction | PASS |
| Restart-safe processing | Crash after decision commit recovers without duplicate decision audit | PASS |
| Human questions | Durable matching interrupt resume; no answer echoed outbound | PASS |
| Uncertain delivery | Process-death recovery marks UNKNOWN_RESULT without resend | PASS |
| Review integrity | Changed payload and expired approval cannot approve | PASS |
| Durable acknowledgment | Storage failure returns Bolt failure rather than successful acknowledgment | PASS |

All normal tests block outbound sockets and DNS. The live runner was not invoked.

## Known limitations

- Live Slack permissions, actual Socket Mode delivery and provider timing remain untested.
- SQLite contention can exceed Slack's acknowledgment deadline; duplicate delivery is
  tolerated, but this is not a distributed broker or an exactly-once provider guarantee.
- UNKNOWN_RESULT and FAILED outbound sends require operator investigation; automatic
  reconciliation/resend administration is not implemented.
- Expired or changed-payload approvals reject callbacks but remain PENDING. Full review
  packet generation, expiration/revocation/consumption lifecycle and submission execution
  belong to later phases.
- Exhausted processing retries require investigation; the associated inbox may remain PENDING.
- Credential pattern filtering is not a universal secret detector. Accepted URLs and
  answers remain sensitive local data. Review and database directories must remain trusted.
- URL intake only queues bounded URLs. No retrieval, qualification or later-phase agents run.

See `docs/slack-control-plane.md` for the API, setup and recovery details.
