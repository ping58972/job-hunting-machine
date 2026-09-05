# Phase 0 implementation report

Date: 2026-09-05  
Project: `/Users/ping58972/Documents/job-hunting-machine`  
Contract: Architecture v2, especially sections 6, 8, 9, 67, 77, 81, 83, and 96  
Status: **Phase 0 acceptance passed**

## Summary

Implemented the Python repository foundation with a src-layout package, a locked
uv environment, Pydantic configuration, a read-only Typer CLI, JSON console logging,
guarded local writes, centralized UTC clocks, and architecture-compatible ULIDs.
The installed SQLAlchemy and Alembic dependencies are ready for the next requested
phase; no database, schema, task execution, or external integration was implemented.

Read the existing AGENTS.md and all 4,580 lines of Architecture v2 before coding.
The architecture was initially found at the misspelled `docs/achitecture-v2.md`.
Its contents were copied to `docs/architecture-v2.md` and checked with `cmp` before
implementation. The canonical filename is present at completion. No architectural
amendment was made.

## Files created

```text
.env.example
.gitignore
.python-version
README.md
pyproject.toml
uv.lock
config/runtime.yaml
config/logging.yaml
docs/architecture-v2.md                 # canonical architecture filename
docs/phase-reports/phase0-report.md
src/job_hunting_machine/__init__.py
src/job_hunting_machine/__main__.py
src/job_hunting_machine/cli.py
src/job_hunting_machine/clock.py
src/job_hunting_machine/config.py
src/job_hunting_machine/ids.py
src/job_hunting_machine/runtime.py
src/job_hunting_machine/py.typed
src/job_hunting_machine/observability/__init__.py
src/job_hunting_machine/observability/logging.py
src/job_hunting_machine/security/__init__.py
src/job_hunting_machine/security/paths.py
tests/conftest.py
tests/unit/test_cli.py
tests/unit/test_clock.py
tests/unit/test_config.py
tests/unit/test_ids.py
tests/unit/test_logging.py
tests/unit/test_paths.py
```

Local generated development artifacts include `.git/`, `.venv/`, `.uv-cache/`,
`.tmp/`, pytest temporary/cache directories, Ruff/mypy caches, and the wheel/source
distribution in `dist/`. These development outputs are not application records.
All were generated inside the project root. A local Git repository was initialized
on `main`; there is no commit, remote, or publication.

## Files modified

- `AGENTS.md`: retained the original safety and phase rules; added foundation API
  conventions, local temporary/cache setup, and exact validation commands.
- Existing candidate documents were not edited or incorporated into package contents.

## Architecture decisions

1. **Phase separation.** SQLAlchemy 2.x and Alembic are installed dependencies only.
   Engines, ORM domain models, migrations, repositories, audit persistence, and durable
   ID handling remain Phase 1. No placeholders with simulated business behavior were added.
2. **Fixed write boundary.** `PROJECT_ROOT` is the exact architecture root. PathGuard
   can narrow its boundary to an existing contained test directory, but cannot widen it.
   Relative targets resolve from the guard root, independently of the current directory.
3. **Guarded writes.** Reject parent traversal, outside absolute paths, symbolic links
   (including internal and dangling links), existing hard-linked destinations, and
   special-file targets. Walk directory components using POSIX descriptors and
   `O_NOFOLLOW`; write a private temporary file and replace the destination atomically.
   File contents are fsynced. New files/directories have at most `0600`/`0700` permissions.
4. **Validated configuration.** Immutable Pydantic settings load from defaults, YAML,
   optional root-local dotenv, then process environment. Only the documented `JHM_`
   keys are accepted. Dotenv interpolation and parent-directory discovery are disabled.
   Configuration cannot override the project root. CLI errors omit input values.
5. **Runtime intent.** Exact `DRY_RUN`, `STAGING`, and `LIVE` enum values are supported;
   DRY_RUN is the default. `jhm config` reports configured intent and
   `workflow_available: false`. No runner exists. Future LIVE execution still requires
   explicit CLI authorization and the architecture's external-action/approval services.
6. **Centralized time.** An injectable `Clock` protocol, UTC `SystemClock`, immutable
   `FrozenClock`, and ISO-8601 millisecond `Z` formatter avoid scattered wall-clock calls.
   Naive datetimes are rejected. Epoch arithmetic uses integers instead of floats.
7. **Centralized IDs.** ULIDs encode a 48-bit timestamp in milliseconds and 80 random
   bits using 26 Crockford Base32 characters. Cryptographic entropy is injectable for
   deterministic tests. TASK, APP, JOB, APR, ART, CNT, and EVT prefixes are centralized.
   Generating an ID does not create or execute a task. Persistence belongs to Phase 1.
8. **Structured logging.** A package-owned JSON stream logger emits timestamp, level,
   task_id, application_id, agent, event, status, duration (seconds), and error_class.
   Missing context is null. Freeform messages, interpolation arguments, unknown fields,
   and exception/stack text are omitted. Reconfiguration does not duplicate handlers.
9. **Local, repeatable tooling.** Python 3.12 is the development pin; package metadata
   requires Python 3.12+. uv.lock pins runtime/development dependencies. Development
   caches and temporary files remain local. Package builds explicitly include only
   source, tests, configuration, documentation, and repository metadata files.

## Database migrations

**None.** Neither `data/job-hunting.db` nor `data/langgraph-checkpoints.db` was created.
There are no manual schema writes, ORM tables, seed rows, audit rows, or Alembic
revision scripts in Phase 0. Future schema changes must use Alembic.

## Commands run

All commands ran from the project root. Installed CPython 3.12.14 was selected.
The main reproducible setup and validation commands were:

```bash
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv sync --locked --python /opt/homebrew/bin/python3.12
uv run --locked ruff format .
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv build
uv run --locked jhm version
uv run --locked jhm config
uv run --locked python -m job_hunting_machine version
git init -b main
git status --short --untracked-files=all
```

Additional implementation checks:

- Read-only `rg`, `cat`, `sed`, `wc`, and `ls` inventory/instruction checks;
  canonical architecture `cp -p` and `cmp` check.
- Initial `uv sync --python /opt/homebrew/bin/python3.12` generated the lockfile.
- Targeted pytest/Ruff/mypy runs for PathGuard, clock/IDs/logging, and configuration/CLI,
  with separate `.pytest-tmp-*` basetemps during parallel work.
- `git check-ignore` verified candidate documents, `.env`, databases, authentication
  state, virtual environment, and development caches are ignored.
- Python `zipfile`/`tarfile` inspection verified package contents exclude candidate
  documents, credentials, databases, caches, and Git state. The wheel includes `py.typed`.
- Dependency imports verified Alembic 1.19.2, SQLAlchemy 2.0.52, Pydantic 2.13.5,
  and Typer 0.27.2 without creating a database.

During development an early editable build ran before README existed and failed;
the completed README resolved it. Initial Ruff findings were corrected before final
validation. The final checks below all exited successfully. uv build warns that its
local cache could be included; archive inspection confirmed the explicit source
distribution allowlist excludes it.

## Acceptance-test results

Final full run: **85 passed, 0 failed, 0 skipped**, Python 3.12.14, pytest 9.1.1.

| Test group | Passed |
| --- | ---: |
| PathGuard | 22 |
| Configuration and runtime defaults | 26 |
| Clock | 4 |
| IDs | 16 |
| Structured logging | 11 |
| CLI | 6 |
| **Total** | **85** |

| Required acceptance scenario | Evidence | Result |
| --- | --- | --- |
| Allowed path succeeds | `test_allowed_path_succeeds` creates/reads a nested UTF-8 artifact | PASS |
| Parent traversal rejected | `test_parent_traversal_rejected`, including a path that would normalize inside | PASS |
| External absolute path rejected | `test_external_absolute_path_rejected` targets outside the real project root | PASS |
| Symlink escape rejected | `test_symlink_escape_rejected`; outside sentinel remains unchanged | PASS |
| Runtime defaults to DRY_RUN | Defaults, shipped YAML, and CLI inspection tests | PASS |
| Task/Application IDs match architecture regex | `test_task_and_application_ids_match_architecture_regex` | PASS |

Additional regressions cover broken/internal symlinks, a parent symlink substituted
after validation, hardlink aliases, permissions, missing parents, immutable roots,
unsafe platforms, configuration precedence, malformed input, log filtering, repeatable
logging setup, UTC formatting, ULID bit layout, and 1,000 IDs generated concurrently.
Socket connection and DNS APIs are blocked by autouse test fixtures. Test attack
sentinels use sibling directories inside the real project root; no outside test files
were created.

| Other check | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 21 files already formatted |
| `mypy` | Strict check passed; 18 source files checked |
| `uv build` | Source distribution and wheel built successfully |
| Package-content inspection | Private files/caches excluded; typing marker included |
| CLI smoke | Version 0.1.0; DRY_RUN configuration; workflows unavailable |
| Independent architecture/security review | No blocking findings |

## External operations and AI usage

- No OpenAI, Slack, Gmail, browser, LinkedIn, or job-site calls.
- No submissions, messages, candidate-data transmission, or external mutations.
- Package setup downloaded Python dependencies from the package index. No credentials
  or application integrations were configured.
- Implemented application model calls: **0**. Application AI/API spending: **$0**.
  Coding-assistant usage is separate and is not measured by this repository report.

## Security concerns and known limitations

- PathGuard is an application API, not an operating-system sandbox. Its helpers must
  be used for application file writes. `validate_write()` alone does not secure a later
  unguarded write. Future database/browser/document libraries need equivalent confinement.
- Guarded write helpers require POSIX no-follow directory descriptor support and fail
  closed on unsupported platforms. Verified on macOS; other operating systems and
  Python versions above 3.12 were not exercised in this phase.
- The project and ancestor directories must remain trusted. An adversary moving an
  already-open directory outside the root or changing mounts is outside this boundary.
- File replacement is atomic, but parent directory entries are not fsynced; full
  power-loss durability is not claimed. A terminated write may leave a private temporary
  file. Later artifact/recovery work must address durability requirements explicitly.
- Logs require static, non-sensitive event/agent/status tokens. Field filtering cannot
  recognize every secret placed inside an allowed token field. No persistent log sink
  or database audit store exists yet.
- ULID uniqueness is probabilistic with cryptographic entropy. IDs sort by timestamp
  millisecond; same-millisecond order is random and clock rollback is not hidden.
  Persistence/replay guarantees are intentionally deferred.
- The installed package uses the fixed architecture checkout for configuration. Moving
  the project requires an explicit architecture amendment. This is a local foundation,
  not the completed machine or a LIVE-ready workflow service.

**Next phase has not started.**
