# Phase 10 Implementation Report

## Scope

Phase 10 implements public company-contact discovery, evidence capture, deterministic contact
ranking, evidence-honest email and LinkedIn-manual drafts, Gmail draft creation, exact-content
SEND_EMAIL approval, and a dedicated idempotent Outreach Sender. Phase 11 monitoring was not
started. Automated tests use fake readers, fake Slack, and fake Gmail without external network.

## Files created

- `src/job_hunting_machine/database/repositories/outreach.py`
- `src/job_hunting_machine/outreach/__init__.py`
- `src/job_hunting_machine/outreach/types.py`
- `src/job_hunting_machine/outreach/discovery.py`
- `src/job_hunting_machine/outreach/drafts.py`
- `src/job_hunting_machine/outreach/email.py`
- `src/job_hunting_machine/outreach/gmail.py`
- `src/job_hunting_machine/outreach/actions.py`
- `src/job_hunting_machine/outreach/connector.py`
- `src/job_hunting_machine/outreach/sender.py`
- `src/job_hunting_machine/outreach/manual.py`
- `src/job_hunting_machine/outreach/cli.py`
- `tests/integration/test_outreach.py`
- `docs/outreach.md`
- `docs/phase-reports/phase10-report.md`

## Files updated

- `src/job_hunting_machine/database/repositories/__init__.py`
- `src/job_hunting_machine/submission/service.py`
- `src/job_hunting_machine/slack/control.py`
- `src/job_hunting_machine/slack/messages.py`
- `src/job_hunting_machine/cli.py`
- `tests/unit/test_cli.py`
- `AGENTS.md`
- `README.md`
- `.env.example`
- `pyproject.toml`

No migration was added. Architecture v2 revision `0001_architecture_v2` already owns the
`contacts`, `outreach_drafts`, `approvals`, `external_actions`, task, and audit tables.

## Architecture decisions

- Confirmed submission now atomically advances the pipeline to `POST_SUBMISSION` and creates one
  idempotent `CONNECT_CONTACTS` task. Phase 11's monitor task remains out of scope.
- The live contact reader is read-only, HTTPS-only, public-address-only, redirect/size bounded,
  and explicitly rejects LinkedIn. Captured HTML is root-confined and hashed before contact rows
  reference the public source through audited provenance.
- Extraction retains literal structured values and `mailto:` addresses. It never manufactures a
  name or title. Missing recruiter attributes remain null.
- Contact email and LinkedIn identifiers normalize and deduplicate per Application ID inside the
  serialized writer transaction. Conflicting public identities fail instead of merging guesses.
- Contacts are ranked deterministically by recruiter, hiring manager, HR, employee, other;
  confidence and stable identifiers break ties before draft generation.
- Draft text uses only the persisted company, position, application, and verified public contact.
  It adds no candidate accomplishment or recruiter claim. LinkedIn output is `LINKEDIN_MANUAL`.
- The Connector receives a capability-restricted external-action facade with Gmail draft creation
  only. It has no send method. No LinkedIn browser/send adapter exists.
- Gmail draft creation is a reversible external action with
  `gmail-draft:<outreach-id>:<payload-hash>`. Unknown outcomes are never blindly repeated.
- SEND_EMAIL canonical JSON binds recipient, subject, exact body, and attachment artifact IDs,
  filenames, MIME types, and SHA-256 hashes. The waiting task, approval, and draft binding are one
  transaction. Changed draft or attachment bytes revoke the approval.
- Slack renders email/manual review from a closed template. Callbacks record an authorized
  decision and resume only the correlated task; they cannot send.
- Only `OutreachSender` calls the Gmail send port. It requires `LIVE`, a current authorized user,
  an unexpired approved exact payload, a confirmed Gmail draft, and the unique
  `send-email:<outreach-id>:<payload-hash>` ledger key.
- A confirmed send records the draft `SENT` and action `SUCCEEDED`. A consumed successful action
  replays as success without another provider call. Executing/unknown actions remain blocked from
  automatic resend.
- Public reads, Gmail writes, Slack transport, and irreversible email sends retain independent
  environment and CLI gates. Defaults remain offline.

## Acceptance results

The Phase 10 integration suite verifies:

1. duplicate public contact records produce one normalized contact;
2. email and LinkedIn-manual drafts retain the correct Application ID, job title, and company;
3. a public email without a literal name/title leaves both values null;
4. a pending/unapproved SEND_EMAIL decision produces no send call;
5. body mutation after approval records `REVOKED` and produces no send call;
6. an approved exact payload sends once through fake Gmail and records `SENT`;
7. replaying the same Slack callback and worker cannot send again.

Additional assertions verify root-local public evidence, deterministic contact classification,
Gmail draft creation, SEND_EXTERNAL_MESSAGE review for LinkedIn-manual drafts, and that the
Connector's injected capability has no send method. Existing Phase 0-9 tests remain green.

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

- Pytest: 371 passed, 0 failed.
- Phase 10 integration module: 7 passed, covering all required scenarios.
- Ruff lint: passed.
- Ruff format check: passed.
- mypy strict type checking: passed for 128 source files.
- Git whitespace check: passed.

## Known limitations

- Strict extraction supports marked public contact blocks and literal mail links. Other page
  layouts safely produce fewer contacts until a source-specific read-only parser is added.
- Contact deduplication relies on SQLite's immediate serialized writer transaction because the
  authoritative Architecture v2 table has no unique contact-identity constraint.
- Gmail OAuth acquisition and refresh are operator-managed. The live adapter accepts a short-lived
  process token and does not persist credentials.
- An ambiguous Gmail draft or send outcome is preserved as `UNKNOWN_RESULT` and never retried
  automatically. Provider-specific read-only reconciliation remains future work.
- Draft personalization intentionally stays conservative and deterministic. It does not add
  candidate claims beyond verified data or use a model to elaborate text.
- LinkedIn sending is wholly manual. Phase 10 creates reviewed text and a public profile URL only;
  it has no scraping, browser automation, connection-request, or message-send capability.
- Phase 11 Gmail monitoring, portal monitoring, and application-status classification are not
  implemented.
