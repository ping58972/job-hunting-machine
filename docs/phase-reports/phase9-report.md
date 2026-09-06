# Phase 9 Implementation Report

## Scope

Phase 9 implements immutable application review, hash-bound Slack approval, a dedicated
Submission Agent, an idempotent external-action ledger, and conservative reconciliation of
unknown submission results. Phase 10 work was not started. Automated tests never contact or
submit to a real company.

## Files created

- `src/job_hunting_machine/submission/__init__.py`
- `src/job_hunting_machine/submission/types.py`
- `src/job_hunting_machine/submission/review.py`
- `src/job_hunting_machine/submission/service.py`
- `src/job_hunting_machine/submission/worker.py`
- `src/job_hunting_machine/submission/fake.py`
- `src/job_hunting_machine/submission/playwright_adapter.py`
- `src/job_hunting_machine/submission/cli.py`
- `tests/integration/test_submission.py`
- `docs/submission.md`
- `docs/phase-reports/phase9-report.md`

## Files updated

- `src/job_hunting_machine/browser/worker.py`
- `src/job_hunting_machine/browser/fake.py`
- `src/job_hunting_machine/orchestration/queue.py`
- `src/job_hunting_machine/slack/control.py`
- `src/job_hunting_machine/slack/messages.py`
- `src/job_hunting_machine/cli.py`
- `tests/unit/test_cli.py`
- `AGENTS.md`
- `README.md`
- `.env.example`
- `pyproject.toml`

No schema migration was added. Architecture v2 already defines `approvals`,
`external_actions`, application status fields, tasks, task memory, and activity history.

## Architecture decisions

- Form preparation still has no submit operation. Its successful transaction moves the
  application to `READY_TO_REVIEW` and creates a distinct `CREATE_REVIEW` task.
- Review payloads contain job identity, every validated answer, the exact resume and cover-letter
  artifact identifiers and hashes, transcript uploads, the application URL, and a UTC creation
  time. Sorted compact JSON with UTF-8 encoding is the canonical representation.
- Each review is written through `PathGuard` to a versioned private directory. Its SHA-256 is
  stored on both the approval and submission task. Reading rejects missing, oversized,
  noncanonical, or hash-mismatched content.
- The review transaction atomically creates the waiting submission task and its expiring approval.
  The Slack message is a closed template containing correlation IDs and the hash. Its callback
  records the decision and resumes that exact task; it performs no browser action.
- The Submission Agent is the only owner of the final-click protocol. Before a first click it
  checks `LIVE`, current authorized-user membership, approval status and expiry, task/application
  correlation, `READY_TO_REVIEW`, exact review bytes, regenerated current payload equality, and
  the submission action ledger.
- The idempotency key is `submit:<application-id>:<review-sha256>`. A new action is recorded
  `PLANNED`; consuming approval and entering `EXECUTING` happen atomically before the adapter call.
- A confirmed response produces `SUCCEEDED` and `SUBMITTED`. Any exception or ambiguous response
  after the action begins produces `UNKNOWN_RESULT` and `SUBMISSION_UNKNOWN`.
- A retry of an unknown action calls only `reconcile`; it never invokes submit again. Confirmation
  produces `SUBMITTED`. Evidence that no submission occurred returns the application to
  `READY_TO_REVIEW` and creates a new review task. Continued ambiguity remains waiting for human
  directed reconciliation.
- The live CLI requires configured `LIVE`, `--live`, `SUBMISSION_ALLOW_LIVE=1`, and the existing
  live browser gate. Its default invocation only reports disabled capability.
- `FakeSubmissionAdapter` models confirmed, lost-after-click, and unknown-without-submit outcomes.
  A real Chromium test also exercises the local routed fake ATS with exactly one POST and no
  external network.

## Acceptance results

The Phase 9 tests verify:

1. pending/no approval blocks the action;
2. rejected approval blocks the action;
3. an expired approval is recorded `EXPIRED` and blocks the action;
4. a non-allowlisted Slack user cannot approve;
5. an answer changed after approval revokes approval;
6. a resume changed after approval revokes approval;
7. valid approval produces exactly one fake submission;
8. a later worker run after confirmed success does not resubmit;
9. a response lost after the click produces `UNKNOWN_RESULT`;
10. reconciliation that finds portal confirmation produces `SUBMITTED` without a second click;
11. reconciliation that finds no submission returns to review and enqueues a new review task.

Additional coverage verifies the default DRY_RUN boundary and a one-click submission against the
Playwright-routed fake ATS. Existing Phase 0-8 tests remain green.

## Commands run

From the fixed project root with `TMPDIR="$PWD/.tmp"`:

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
git diff --check
```

## Test totals

- Pytest: 364 passed, 0 failed.
- Phase 9 integration module: 13 passed, including all 11 required scenarios.
- Ruff lint: passed.
- Ruff format check: passed.
- mypy strict type checking: passed for 115 source files.
- Git whitespace check: passed.

## Known limitations

- Live ATS confirmation is necessarily portal-specific. The conservative generic Playwright
  adapter requires one unambiguous enabled final-submit control and recognized confirmation text;
  unfamiliar markup becomes unknown or blocks rather than guessing.
- Reconciliation uses the currently persisted browser storage state. Expired accounts, CAPTCHA,
  MFA, or an unavailable confirmation page require human recovery.
- Review files are application-level immutable records: the system never edits an existing review
  version and detects external byte changes, but it does not provide filesystem append-only flags
  against a privileged local process.
- Slack delivery remains separately opt-in. If a Slack notification is not delivered, the durable
  approval and waiting task remain available for safe retry.
- Phase 9 does not implement post-submission email, status monitoring, outreach, or Phase 10 work.
