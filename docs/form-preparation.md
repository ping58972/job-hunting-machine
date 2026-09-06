# Browser and Form Preparation

Phase 8 prepares an application form and stops at `READY_TO_REVIEW`. The Form Agent has no
final-submit operation. It detects final application controls during inspection and completes the
task without activating them.

## Safety boundary

`DRY_RUN` opens no browser and performs no mutation. `STAGING` accepts only the routed local fake
ATS at `https://fake-ats.invalid`; Playwright fulfills every request locally, so tests use a real
Chromium process without network access. Real sites require all three gates: configured `LIVE`, the
`jhm form worker --live` flag, and `FORM_BROWSER_ALLOW_LIVE=1`. Slack and ModelGateway retain their
own separate opt-ins.

Every form fill and upload goes through `browser.actions.ExternalActionService`. The service checks
the live task lease, exact Task/Application IDs, `PREPARE_APPLICATION` approval, approval expiry,
payload file hash, request hash, and idempotency key. Approval covers the canonical values and exact
attachment IDs and hashes for one page plan. A changed plan revokes an unused approval and requires
a new decision. Slack callbacks only persist the decision and resume the exact task.

CAPTCHA, MFA, sensitive or secret-like fields, missing attachments, ambiguous questions, unsafe
navigation, and Workday/iCIMS/SmartRecruiters mutation all pause for human handling. Greenhouse,
Lever, Ashby, and Generic adapters use accessible labels, roles, stable attributes, and semantic
field keys. Old selectors and DOM indexes are never persisted as recovery state.

## Durable state

Canonical information is stored in `my_information_for_filling_form`; only verified values with an
allowed policy can autofill. `form_answers` records the application, page, field, source, confidence,
verification time, and status before browser mutation. Authorized non-sensitive Slack replies keep
user/event provenance and update a registered canonical key only when its policy permits this.

The exact resume comes from `application_details.resume_artifact_id`. Optional cover letters come
from the corresponding application detail. A transcript must be explicitly supplied as an
application-scoped `TRANSCRIPT` artifact in the task payload. Every file is root-confined and its
SHA-256 is checked again before use.

Playwright storage state is retrieved as a dictionary with IndexedDB, serialized canonically, and
written through `PathGuard` beneath `data/browser-sessions/<application-id>/storage-state.json`.
Files are private and ignored by Git. Recovery restores the dictionary, revisits the saved URL,
re-inspects the live DOM, and reconciles the external-action ledger. Cookies and tokens never enter
logs or task memory.

## Commands

```bash
uv run --locked jhm form worker
uv run --locked jhm form worker --staging --once --database .tmp/form-demo.db
```

The first command is an offline capability report. The second uses only the fake ATS. A live command
can prepare fields through the review boundary after all explicit gates and approvals; it still
cannot submit an application.
