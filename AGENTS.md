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
Phase 0 has no database schema, workflow runner, or external integration.

LOCAL DEVELOPMENT:
Run from the project root with Python 3.12+ and uv.
Before setup/checks: mkdir -p .tmp; export TMPDIR="$PWD/.tmp"
uv cache and pytest temporary files are configured inside the project root.
Run uv run --locked pytest, uv run --locked ruff check .,
uv run --locked ruff format --check ., and uv run --locked mypy.
Keep candidate source documents, credentials, databases, and generated data out of Git.
