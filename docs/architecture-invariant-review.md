# Architecture v2 Invariant Review

Review date: 2026-09-06. Scope: the local repository through Phase 12. Architecture v2 remains
authoritative. This review combines source inspection, the automated `jhm doctor` checks, the
versioned fault matrix, and the full network-free test suite.

| Invariant | Result | Evidence and remaining boundary |
| --- | --- | --- |
| INV-001 Project root | PASS | `PathGuard` fixes the root, rejects traversal and symlink escapes, and guarded writers are used for workflow files. Artifact, approval, candidate-evidence, and browser-session paths are checked by `doctor`. |
| INV-002 SQLite authoritative | PASS | `data/job-hunting.db` owns application state. Reports read SQLite; no mirror is used for decisions. |
| INV-003 Separate checkpoints | PASS | LangGraph uses `data/langgraph-checkpoints.db` through the lease-fenced saver. Alembic owns only the application database. Backup and doctor treat the files separately. |
| INV-004 Structured state is memory | PASS | Durable workers reconstruct state from task memory, domain rows, action ledgers, evidence, and checkpoints. Process-death recovery tests confirm restart behavior. |
| INV-005 Unique Task ID | PASS | Central prefixed ULID generation and database primary/unique constraints protect Task IDs. The queue requires persisted tasks before execution. |
| INV-006 Unique Application ID | PASS | Central prefixed ULIDs and application primary keys protect Application IDs. Post-qualification tasks carry the exact application correlation. |
| INV-007 No invented candidate facts | PASS | Candidate facts require provenance and explicit verification. Retrieval and local LaTeX resume planning read current VERIFIED facts only. `doctor` checks evidence presence. Human review remains responsible for truthfulness. |
| INV-008 Canonical form answers | PASS | The resolver accepts canonical verified information, verified candidate facts, approved derived values, or exact artifacts. Unknown and sensitive fields pause. |
| INV-009 Submission approval | PASS | Only the dedicated Submission Agent can click final submit, after LIVE gates, an unexpired authorized approval, matching review hash/state, and idempotency checks. |
| INV-010 Outreach approval | PASS | Only the Outreach Sender can send email, after LIVE gates and a current SEND_EMAIL approval. LinkedIn remains manual. |
| INV-011 Exact-content approval | PASS | Canonical payload hashes bind form preparation, submission, and email content. Changed answers, artifacts, recipients, subjects, bodies, or attachments revoke/block use. |
| INV-012 Dedicated executors | PASS | Form adapters expose no submit callable. Connector code cannot send. Static doctor inspection and acceptance tests enforce the boundaries. |
| INV-013 Idempotent external effects | PASS | External actions require stable idempotency keys. Unknown submission/email outcomes enter reconciliation and are never blindly replayed. `doctor` rejects irreversible unapproved or empty-key rows. |
| INV-014 DRY_RUN default | PASS | Pydantic runtime settings and committed runtime YAML default to DRY_RUN. Each live transport has an additional flag and environment gate. |
| INV-015 No CAPTCHA bypass | PASS | CAPTCHA and MFA are detected before mutation and move the exact task to `WAITING_HUMAN`; fake ATS tests observe no challenge interaction. |
| INV-016 No LinkedIn browser automation | PASS | Outreach produces manual LinkedIn drafts only. The browser package contains no LinkedIn target, and `doctor` checks this source boundary. |
| INV-017 Budgeted model use | PASS | OpenAI SDK imports are confined to `models/client.py`; ModelGateway handles registry, budget reservations, structured output, retry/escalation, and durable usage. `doctor` checks task binding. |
| INV-018 Deterministic code first | PASS | URL handling, extraction, policy rules, dates, salaries, dedupe, hashing, page count, state transitions, scheduling, and exact status phrases are deterministic. Model calls are bounded ambiguity fallbacks. |

## Entire-machine definition of done

The network-free implementation evidence satisfies every Architecture v2 checklist item: root
confinement; SQLite and LangGraph restart; stale and duplicate recovery; audited transitions;
ModelGateway routing and cost records; editable qualification policy; conservative authorization
handling; verified-only one-page resumes; safe form and CAPTCHA pauses; browser recovery;
approval-bound and idempotent submission/email; unknown-result reconciliation; manual LinkedIn;
terminal monitor exclusion; meaningful Slack changes; DRY_RUN and explicit LIVE gates; fault
coverage; backups; and recovery documentation.

Architecture Amendment A1 additionally verifies marker-scoped template edits, LaTeX escaping,
direct compiler invocation without a shell, one-page PDF enforcement, TEX/PDF artifact pairing,
legacy DOCX read compatibility, and exact PDF approval binding. Google Docs is absent from the
current application-document runtime.

This does **not** make the machine safe to enable in LIVE mode today. The automated suite uses
fake Slack, Gmail, browser/ATS, GitHub, local LaTeX compiler, and model transports. It does not validate the
operator's current credentials and scopes, real ATS markup, account security challenges, provider
rate limits, or a controlled real-world approval/action/reconciliation exercise. `jhm doctor`
therefore reports `safe_to_enable_live: false`, even when all local architecture checks pass.
Runtime remains DRY_RUN until a human deliberately changes configuration and separately supplies
every provider-specific command flag and environment opt-in.

## Automated review boundary

`jhm doctor` checks database integrity, foreign keys, migration revision, WAL, busy timeout,
checkpoint integrity when present, synchronized application status, external-action authorization,
model-usage task binding, artifact and approval hashes, verified-fact evidence, browser-state
permissions, OpenAI import confinement, Form Adapter submit absence, LinkedIn browser absence,
DRY_RUN defaults, and all four evaluation datasets. It exits with status 2 on a failed invariant.
Warnings identify optional state such as a checkpoint database that has not yet been created.
