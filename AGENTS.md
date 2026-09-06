PROJECT ROOT:
Only modify files under /Users/ping58972/Documents/job-hunting-machine.

ARCHITECTURE:
Read docs/architecture-v2.md before making architectural changes.

SAFETY:
No live submission in tests.
No live email sends in tests.
No unauthorized LinkedIn automation.
No invented candidate facts.
No CAPTCHA bypass.

DATABASE:
Use Alembic migrations.
Never manually mutate production DB schema.
Every state transition must be auditable.

AI:
Use ModelGateway only.
Agents may not call OpenAI directly.
Use structured outputs.
Record model usage.

EXTERNAL ACTIONS:
All external mutations use ExternalActionService.
Irreversible actions require ApprovalService.

TESTING:
DRY_RUN is default.
Never enable LIVE automatically.

PHASE CONTROL:
Implement only the requested phase.
Do not start the next phase.
Provide an implementation report and acceptance-test results.

FOUNDATION CONVENTIONS:
Architecture v2 is authoritative at docs/architecture-v2.md.
Use the src/job_hunting_machine package and centralized RuntimeMode, Clock,
IdGenerator, and PathGuard APIs. Use guarded helpers for application file writes;
validate_write alone does not protect a subsequent unguarded write.
Do not make the fixed project-root boundary configurable.
Only use static, non-sensitive event, agent, and status tokens in logs.
Phase 11 owns read-only Gmail and portal monitoring, durable evidence, validated application
status transitions, and meaningful-change notifications; do not start Phase 12 reliability work.
Terminal applications are never scheduled or portal-read. Low-confidence or invalid transitions
remain auditable observations and must not change application state.
Form Agent interfaces must never expose a final-submit operation or activate a submit control.
Require a hash-bound PREPARE_APPLICATION approval before the first browser mutation.
CAPTCHA, MFA, sensitive fields, unsupported ATS portals, and ambiguous fields pause for a human.
Browser storage state is private, root-confined, and must never be logged.
Copy Google Docs natively; edit only PROJECTS and SKILLS in the resume template.
Keep template tables, headers, paragraphs, fonts, margins and protected text intact.
Generated claims must resolve to current VERIFIED facts; never infer metrics.
All document mutations go through resume.actions.ExternalActionService.
Native GDOC pointers require content snapshots and validated PDF artifacts.
Create FORM_PROCESS only in the transaction that records validated artifacts.
Extracted GitHub observations remain UNVERIFIED until explicit audited human review.
Resume retrieval must use current VERIFIED facts with validated source evidence only.
Keep qualification graph nodes pure; fetch/model calls run at durable effect boundaries.
Unknown authorization evidence requires review. Only the dedicated Submission Agent may perform
the final click, after LIVE mode, current authorized-user approval, exact review hash, application
state, and idempotency checks all pass. UNKNOWN_RESULT must reconcile without another click.
Slack callbacks record decisions only; they must never submit applications.
Connector Agent cannot send outreach. Only the dedicated Outreach Sender may send email after a
current SEND_EMAIL approval matches recipient, subject, body, and attachment hashes.
LinkedIn outreach remains a reviewed manual draft; never browse or send through automation.
Slack transport defaults to fake; live Socket Mode requires explicit opt-in.
Route Slack sends through ExternalActionService and keep outbound templates closed.
OpenAI SDK imports and requests belong only in models/client.py, called by ModelGateway.
Default gateway mode is mock; live construction requires OPENAI_ALLOW_LIVE=1 and a key.
Do not release unresolved model budget reservations after a timeout or process death.
Use Database.transaction() and repository methods for audited state changes.
Use QueueService for execution transitions; TaskRepository.update_status is a low-level
persistence primitive and does not enforce lease ownership or transition policy.
All checkpoint writes must use the lease-fenced AsyncSqliteSaver wrapper.
Graph nodes must be replay-safe: no uncheckpointed effects, clocks, random values,
ID generation, or live integration calls. Interrupt nodes restart on human resume.
Apply schema changes only through Alembic; never use Base.metadata.create_all().
Keep LangGraph checkpoint tables out of the application database.

LOCAL DEVELOPMENT:
Run from the project root with Python 3.12+ and uv.
Before setup/checks: mkdir -p .tmp; export TMPDIR="$PWD/.tmp"
uv cache and pytest temporary files are configured inside the project root.
Run uv run --locked pytest, uv run --locked ruff check .,
uv run --locked ruff format --check ., and uv run --locked mypy.
Keep candidate source documents, credentials, databases, and generated data out of Git.
