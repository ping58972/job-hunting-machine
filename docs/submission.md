# Immutable review and submission

Phase 9 separates review, decision recording, and the final external action.
The Form Agent stops at `READY_TO_REVIEW` and enqueues `CREATE_REVIEW`. The review worker
collects validated form answers and exact application-scoped attachments, serializes them as
canonical JSON, stores the private snapshot under `applications/<APP_ID>/review/<ULID>/review.json`,
and hashes the exact bytes with SHA-256.

The same database transaction creates a `SUBMIT_APPLICATION` task in `WAITING_HUMAN` and a
`SUBMIT_APPLICATION` approval tied to the Task ID, Application ID, review path, hash, and expiry.
The Slack outbox renders those identifiers through a closed template. An authorized callback
records `APPROVED` or `REJECTED` and resumes the exact task. It cannot invoke a browser or submit.

The Submission Agent rechecks all of these conditions before its first click:

- runtime mode is `LIVE`;
- the approval is `APPROVED`, unexpired, and attributed to a currently authorized Slack user;
- the task, application, approval, path, and hash correlations match;
- the application is `READY_TO_REVIEW`;
- the stored review bytes remain canonical and match their SHA-256;
- regenerating the review from current answers and artifact bytes produces identical canonical JSON;
- no active submission action or conflicting idempotency key exists.

The stable action key is `submit:<application-id>:<review-hash>`. Immediately before calling the
submission adapter, one transaction consumes the approval, moves the application through the
submission boundary, and marks the ledger action `EXECUTING`. A confirmed portal result records
`SUBMITTED`. Any exception or ambiguous result after the click records `UNKNOWN_RESULT` and
`SUBMISSION_UNKNOWN`.

An unknown action is never clicked again. A resumed worker calls only the adapter's read-only
reconciliation operation. Portal confirmation records `SUBMITTED`; affirmative evidence that no
submission exists records the action `FAILED`, returns the application to `READY_TO_REVIEW`, and
enqueues a fresh immutable review. Ambiguous reconciliation remains unknown.

The default command reports disabled capability and performs no browser action:

```bash
uv run --locked jhm submission worker
```

Live operation requires all gates and current Slack configuration:

```bash
export JHM_RUNTIME_MODE=LIVE
export FORM_BROWSER_ALLOW_LIVE=1
export SUBMISSION_ALLOW_LIVE=1
uv run --locked jhm submission worker --live --once
```

Do not set these values in committed files. Automated tests use `FakeSubmissionAdapter`, block
network access, and never instantiate the live Playwright adapter.
