# Phase 8 Implementation Report

Date: 2026-09-06

## Scope completed

Phase 8 implements approval-bound browser and form preparation through
`READY_TO_REVIEW`. It does not implement Phase 9 review snapshots, submission tasks, final
submission, account-email actions, outreach, CAPTCHA solving, or MFA entry.

## Files created

- `config/form.yaml`
- `docs/form-preparation.md`
- `docs/phase-reports/phase8-report.md`
- `src/job_hunting_machine/browser/__init__.py`
- `src/job_hunting_machine/browser/actions.py`
- `src/job_hunting_machine/browser/adapters.py`
- `src/job_hunting_machine/browser/artifacts.py`
- `src/job_hunting_machine/browser/cli.py`
- `src/job_hunting_machine/browser/config.py`
- `src/job_hunting_machine/browser/detector.py`
- `src/job_hunting_machine/browser/fake.py`
- `src/job_hunting_machine/browser/manager.py`
- `src/job_hunting_machine/browser/resolver.py`
- `src/job_hunting_machine/browser/types.py`
- `src/job_hunting_machine/browser/worker.py`
- `src/job_hunting_machine/database/repositories/forms.py`
- `tests/integration/test_form_browser.py`

The phase also updates `.env.example`, `AGENTS.md`, `README.md`, `pyproject.toml`, the CLI,
repository exports, queue transaction composition, Slack callback provenance/resume handling,
and the affected Phase 4/CLI tests.

## Architecture decisions

1. **No schema redesign.** The existing `0001_architecture_v2` Alembic revision already owns
   form information, form answers, browser sessions, approvals, external actions, and audits.
   Phase 8 adds repositories and does not add or manually create a table.
2. **No submit surface.** `BaseAdapter` exposes inspection, fill, upload, and safe advance only.
   The Form Agent has no final-submit callable. Adapters detect semantic final controls and the
   worker stops at `READY_TO_REVIEW` without activating them.
3. **Explicit runtime gates.** `DRY_RUN` is offline. `STAGING` is restricted to the routed fake
   ATS. Real-site preparation requires configured `LIVE`, `--live`, and
   `FORM_BROWSER_ALLOW_LIVE=1`. Slack and ModelGateway keep their independent opt-ins.
4. **Root-confined browser state.** Playwright storage state is retrieved as a dictionary with
   IndexedDB and restored as a dictionary. Canonical JSON is written through `PathGuard` under
   `data/browser-sessions/<application-id>/`; files are private and Git-ignored. Cookies and
   tokens are not placed in logs or task memory.
5. **One modifying session per domain.** BrowserManager uses an in-process guard plus a private
   nonblocking POSIX file lock. The OS releases the lock after a crash. LIVE URLs must be HTTPS,
   resolve only to public addresses, and cannot target LinkedIn.
6. **Conservative ATS support.** Detection order is Greenhouse, Lever, Ashby, Workday,
   SmartRecruiters, iCIMS, then Generic. Greenhouse, Lever, Ashby, and Generic can prepare forms.
   The other three inspect and pause before mutation.
7. **Evidence-bound answers.** Deterministic aliases run before optional ModelGateway routing.
   Autofill accepts verified canonical form information, current VERIFIED candidate facts,
   authorized per-application user answers, or validated file artifacts. An unfamiliar-question
   model can select an existing key or request a human; it cannot provide a new value.
8. **Policy-safe human answers.** Slack-safe questions use the existing closed message template.
   Replies persist the authorized Slack user and event provenance, resume the exact Task ID, and
   update canonical information only for a registered normal key whose policy allows it. Sensitive,
   secret-like, manual-only, and unmapped information is not added to the canonical table.
9. **Exact artifact selection.** Resume selection is bound to
   `application_details.resume_artifact_id`. Cover letters use the matching application detail.
   Transcripts require an explicitly supplied, application-scoped `TRANSCRIPT` artifact. Type,
   Application ID, root path, regular-file metadata, size, and SHA-256 are checked. Playwright
   receives the verified bytes as a file payload instead of reopening an unchecked path.
10. **Approval before mutation.** A canonical PREPARE_APPLICATION payload binds Task ID,
    Application ID, ATS, URL, page, field values, source references, attachment IDs, and hashes.
    The private payload file and SHA-256 back an exact approval. Plan changes revoke an unused
    approval and require a new one.
11. **Callbacks decide and resume only.** Approval callbacks update the durable approval and resume
    its matching interrupt. They cannot reach BrowserManager or an adapter. Rejection, expiration,
    payload changes, and correlation failures block browser writes.
12. **Audited idempotent effects.** Every fill and upload is lease-fenced and stored in
    `external_actions` with a stable task/page/field/value key before execution. Confirmed replays
    reconcile the DOM. Reloaded controls may reapply the exact reversible operation under the same
    consumed approval. Unknown outcomes are not blindly repeated.
13. **Durable recovery.** Browser URL/page checkpoints, storage state, semantic FormAnswer rows,
    task memory, approvals, and the external-action ledger survive process/database restart.
    Recovery re-inspects the current DOM and does not depend on stale selectors or DOM indexes.
14. **Audited business transitions.** Missing information and challenges set
    `WAITING_USER_INPUT`; confirmed preparation sets `FORM_IN_PROGRESS`. Reaching the final review
    boundary atomically sets application status `READY_TO_REVIEW`, pipeline stage `REVIEW`, and the
    leased task `SUCCEEDED`. Failures before a confirmed browser action do not advance business
    state.
15. **Replay-sensitive isolation.** Browser IO, model calls, clocks, ID generation, approvals,
    and file writes remain at durable effect boundaries. The existing lease-fenced queue and
    AsyncSqliteSaver infrastructure is retained; LangGraph checkpoint tables are not added to the
    application database.

## Commands run

All commands ran from the fixed project root after creating `.tmp` and setting
`TMPDIR="$PWD/.tmp"`.

```text
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
git diff --check
uv run --locked jhm form worker
uv run --locked jhm config --env-file .tmp/nonexistent.env
```

Focused checks were also run repeatedly for the Phase 8 browser, Slack, and CLI tests during
implementation.

## Test totals

- Full pytest suite: **351 passed, 0 failed**.
- Phase 8 browser/form integration file: **19 passed**.
- The fake ATS uses a real Chromium process with Playwright route fulfillment and no network call.
- The suite verifies the review boundary without a submit request, single-page submit-control
  handling, ATS detection/support levels, unknown fields, Slack answer provenance, rejection,
  CAPTCHA, MFA, storage-state restart and permissions, same-domain exclusion, LIVE gates, exact
  artifact integrity, application state safety, and absence of a submit callable.

## Known limitations

- Workday, iCIMS, and SmartRecruiters are inspect-only hooks and always require human handling
  before mutation.
- Vendor markup changes can make an accessible field or safe advance ambiguous. The worker pauses
  instead of guessing.
- Playwright storage state cannot reconstruct portal state outside cookies, local storage, and
  IndexedDB. Portals that rely on unsupported session state require human recovery.
- CAPTCHA and MFA are detected and paused; Phase 8 provides no solver or bypass.
- Unfamiliar semantic field routing is infrastructure only and depends on explicit ModelGateway
  configuration. Normal tests use no live model call.
- LIVE form preparation was not exercised. Normal tests made no OpenAI, Slack, browser-network,
  email, or application-submission call.
- Phase 8 creates no immutable review snapshot and no submission task; those belong to Phase 9.
