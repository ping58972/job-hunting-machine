# Phase 12 Implementation Report

## Summary

Phase 12 adds network-free reliability coverage, four versioned evaluation datasets, online local
backups, conservative startup recovery, health and integrity diagnostics, operational reports,
and launchd guidance. It also closes fault-handling gaps for expired browser logins, terminated
Chromium processes, missing source resumes, missing transcripts, and audited recovery of abandoned
browser actions.

**The machine is not safe to enable in LIVE mode yet.** All local architecture and fault checks
pass, but the test suite uses fake or mock providers and does not validate current credentials,
account scopes, real ATS markup, provider security challenges, provider rate limits, or a
controlled real-world approval/action/reconciliation exercise. `jhm doctor` therefore reports
`safe_to_enable_live: false`. The committed runtime remains `DRY_RUN`, and Phase 12 does not change
any LIVE opt-in.

## Files created

- `config/fault-injection.yaml`
- `evals/datasets/qualification.jsonl`
- `evals/datasets/job-extraction.jsonl`
- `evals/datasets/status-classification.jsonl`
- `evals/datasets/resume-project-ranking.jsonl`
- `src/job_hunting_machine/reliability/__init__.py`
- `src/job_hunting_machine/reliability/backup.py`
- `src/job_hunting_machine/reliability/recovery.py`
- `src/job_hunting_machine/reliability/reports.py`
- `src/job_hunting_machine/reliability/evals.py`
- `src/job_hunting_machine/reliability/doctor.py`
- `tests/fault_injection/test_phase12.py`
- `docs/local-operations.md`
- `docs/launchd/com.job-hunting-machine.recover.plist.example`
- `docs/launchd/com.job-hunting-machine.monitor-schedule.plist.example`
- `docs/architecture-invariant-review.md`
- `docs/phase-reports/phase12-report.md`

## Files modified

- `src/job_hunting_machine/cli.py`
- `src/job_hunting_machine/browser/actions.py`
- `src/job_hunting_machine/browser/fake.py`
- `src/job_hunting_machine/browser/manager.py`
- `src/job_hunting_machine/browser/worker.py`
- `src/job_hunting_machine/resume/config.py`
- `tests/integration/test_form_browser.py`
- `tests/unit/test_cli.py`
- `AGENTS.md`
- `README.md`
- `.env.example`
- `config/runtime.yaml`
- `pyproject.toml`

## Database migrations

No Alembic migration was added. Architecture v2 revision `0001_architecture_v2` already owns all
application, queue, usage, approval, action-ledger, monitor, browser-session, and audit tables.
Phase 12 reads or updates those tables through existing transaction and repository boundaries.

## Architecture decisions

- `jhm backup` uses SQLite's online backup API for the application and checkpoint databases. It
  verifies each database, stores root-local private copies, and hashes all backed-up files in a
  canonical manifest. Architecture v2's 30-day retention is recorded as an operator recommendation;
  automatic deletion is intentionally absent.
- `jhm recover` requires a clean, compatible application schema, then composes existing queue,
  browser, Slack, submission, and Gmail recovery contracts. It uses fake DRY_RUN executors, leaves
  `WAITING_HUMAN` untouched, and never repeats an uncertain effect. Abandoned effects keep or move
  to their explicit unknown/reconciliation state.
- `jhm status`, `jhm queue`, `jhm applications`, and `jhm costs` are read-only SQLite reports.
  Cost reporting separates settled usage from unresolved conservative reservations.
- `jhm db integrity` checks SQLite integrity, foreign keys, the exact Alembic revision, WAL, and
  busy timeout. `jhm doctor` also checks application-state consistency, artifacts, approvals,
  verified-fact provenance, private browser state, preparation/irreversible-action authorization,
  model usage, source boundaries, DRY_RUN, and evaluation results.
- The doctor is fail-closed. Local passes establish architecture readiness, while LIVE readiness
  stays false until real providers and credentials have been deliberately validated outside the
  automated suite.
- The versioned fault matrix gives each required fault a stable name and points to the exact test
  that injects and verifies it. A test prevents a required mapping or target test from disappearing.
- Evaluation JSONL files use Pydantic validation, unique case IDs, file hashes, and no network.
  Scoring exercises the repository's real deterministic job extraction, qualification, status
  classification, and verified-only project filtering/ranking logic.
- An expired browser-session record pauses before browser access. A disconnected Chromium process
  is disposed and relaunched before root-confined storage state is restored.
- A missing source resume fails validation before document work. A required transcript without an
  exact application artifact pauses before upload or any browser external action.
- Recovered abandoned browser mutations now append an audit event when they become
  `UNKNOWN_RESULT`.
- The macOS launchd examples run only local recovery and monitor scheduling. They contain no LIVE
  flag, provider allow flag, credential, submission worker, or email sender.

## Fault-injection and end-to-end acceptance

| Required fault | Covered behavior |
| --- | --- |
| laptop/process termination | A killed child worker resumes from the saved LangGraph node. |
| stale worker lease | The expired owner is fenced and a new owner recovers the task. |
| SQLite busy/locked | Lock contention is classified and handled by the durable retry policy. |
| OpenAI timeout | Transport retries are bounded and usage attempts persist without unsafe tier escalation. |
| malformed structured output | Schema failure is bounded and follows the configured Luna-to-Terra route. |
| Slack duplicate delivery | Concurrent duplicates produce one durable inbox task. |
| Slack unavailable | An unknown send result is retained and is not automatically resent. |
| browser crash | A disconnected Chromium process is replaced cleanly. |
| expired browser login | The task becomes `WAITING_HUMAN` before browser access. |
| CAPTCHA | The task pauses and the fake ATS observes no challenge interaction. |
| Gmail unavailable | The monitor enters durable retry without changing business state. |
| network loss before submit | Reconciliation finding no submission returns the application for review. |
| network loss immediately after submit | The action becomes `UNKNOWN_RESULT`; there is no second click. |
| duplicate approval callback | Callback replay cannot resend approved outreach. |
| changed payload after approval | Changed form data invalidates the submission approval. |
| corrupted artifact | Hash mismatch fails before browser/session or business-state mutation. |
| missing source resume | Resume policy validation fails closed. |
| missing transcript | The exact task pauses before any upload or action-ledger row. |
| duplicate job | Canonical URL replay creates one job and one task. |
| deleted job posting | Removed-page evidence is retained and classified without application creation. |
| application status conflict | Doctor reports the details/pipeline mismatch as a failed invariant. |

The dedicated Phase 12 module also verifies online backup integrity and state preservation,
single-coordinator stale recovery, preservation of human waits, refusal to recover an incompatible
database, all required operator commands, read-only reporting, evaluation totals, and the explicit
negative LIVE verdict.

## Evaluation results

- Qualification: 4 cases passed.
- Job extraction: 3 cases passed.
- Status classification: 4 cases passed.
- Resume project ranking: 2 cases passed.
- Total: 13 passed, 0 failed across 4 versioned datasets.

The project-ranking cases include an unverified candidate that must be filtered. The qualification
cases execute the deterministic Architecture v2 country, paid, salary, application-open, and
authorization handling. Extraction and status cases execute their production deterministic
classifiers.

## Commands run

From the fixed project root with `TMPDIR="$PWD/.tmp"`:

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
git diff --check
uv lock --check
plutil -lint docs/launchd/com.job-hunting-machine.recover.plist.example
plutil -lint docs/launchd/com.job-hunting-machine.monitor-schedule.plist.example
```

The following commands were also exercised against a newly migrated isolated database:

```bash
uv run --locked jhm db init --database .tmp/phase12-cli-verify.db
uv run --locked jhm status --database .tmp/phase12-cli-verify.db
uv run --locked jhm queue --database .tmp/phase12-cli-verify.db
uv run --locked jhm applications --database .tmp/phase12-cli-verify.db
uv run --locked jhm costs --database .tmp/phase12-cli-verify.db
uv run --locked jhm recover --database .tmp/phase12-cli-verify.db
uv run --locked jhm db integrity --database .tmp/phase12-cli-verify.db
uv run --locked jhm doctor --database .tmp/phase12-cli-verify.db
uv run --locked jhm backup --database .tmp/phase12-cli-verify.db
```

## Test and check results

- Pytest: 402 passed, 0 failed.
- Phase 12 fault/operator module: 14 passed.
- Required fault matrix: 21 scenarios mapped and green in the full suite.
- Evaluation runner: 13 cases passed, 0 failed across 4 datasets.
- Ruff lint: passed.
- Ruff format check: passed.
- mypy strict type checking: passed for 148 source files.
- Git whitespace check: passed.
- uv lock verification: passed.
- Both launchd plist examples: valid.
- Fresh isolated database: migration and 17 policy seeds succeeded; integrity was `ok`, foreign-key
  violations were 0, revision was `0001_architecture_v2`, journal mode was WAL, and busy timeout was
  5,000 ms.
- Fresh doctor run: 0 failures, 1 warning because the optional checkpoint database is created only
  when a worker first starts; architecture readiness true; LIVE safety false.

## External operations and cost

No live OpenAI, Slack, Gmail, GitHub, Google Docs, portal, company ATS, email-send, LinkedIn, or
application-submission operation ran. Browser tests used routed local fake ATS pages. Model tests
used mock clients. Estimated live AI/provider cost for this phase is $0.00.

## Security concerns and known limitations

- LIVE provider readiness is unverified. Credentials, OAuth scopes, Slack allowlists, real browser
  sessions, live model budgets, ATS-specific selectors, rate limits, and provider recovery behavior
  need an explicit operator-controlled readiness exercise before any LIVE use.
- `jhm doctor` intentionally cannot return a positive LIVE safety verdict. It proves local
  architecture invariants and explains the missing live validation boundary.
- Backup retention is operator-managed. The command does not encrypt backups or copy them off-site;
  filesystem and device encryption remain operator responsibilities.
- The backup covers the databases, configuration YAML, prompts through `config/`, Architecture v2,
  and critical build metadata. Large generated artifacts/evidence remain referenced by database
  hashes and are not duplicated into each backup.
- Checkpoint integrity is a warning before any worker has created the checkpoint database. Once it
  exists, an integrity failure is fatal.
- launchd examples assume Homebrew `uv` at `/opt/homebrew/bin/uv` and this machine's fixed project
  path. They must be reviewed if the local tool path changes. They enqueue monitoring work but do
  not start a live monitor reader or any irreversible executor.
- Evaluation datasets are small regression sets, not statistical quality claims. Expand them with
  reviewed real-world, redacted examples before relying on measured model quality.
- SQLite remains a single-host design. The 5-second busy timeout and retry policy handle bounded
  contention; they do not make the database suitable for distributed writers.

The complete INV-001 through INV-018 assessment is in
`docs/architecture-invariant-review.md`. Next phase has not started.
