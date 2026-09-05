# Job Hunting Machine

Phase 0 establishes the local Python foundation for [Architecture v2](docs/architecture-v2.md).
It provides safe file writes, validated configuration, UTC clocks, ULID identifiers,
structured logging, and a small inspection CLI. No job-search workflow runs yet.

## Setup

Use Python 3.12 or newer and `uv`. The development interpreter is pinned to 3.12.
The project root is fixed by the architecture:

```bash
cd /Users/ping58972/Documents/job-hunting-machine
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv sync --locked
uv run --locked jhm version
uv run --locked jhm config
```

Install Python separately if 3.12 is unavailable; automatic Python downloads are
disabled. `uv` stores dependencies in `.venv` and its cache in `.uv-cache` inside
this repository. The temporary-directory setting keeps build temporary files local.
For a different installed supported interpreter, pass `--python /path/to/python` to
`uv sync`. Commit `uv.lock` so other installations use the tested dependency versions.

## Configuration

Configuration precedence, from lowest to highest, is:

1. Pydantic defaults: `DRY_RUN` and `INFO`.
2. `config/runtime.yaml` and `config/logging.yaml`.
3. The optional project-root `.env` file.
4. Process environment variables.

Only `JHM_RUNTIME_MODE` and `JHM_LOG_LEVEL` are supported. Unknown `JHM_` variables,
unknown YAML fields, invalid values, and unsafe file paths fail validation. Relative
configuration paths resolve against the project root, independent of the working
directory. No parent-directory dotenv search or variable interpolation occurs.
Copy `.env.example` to `.env` if local overrides are needed; keep secrets out of YAML
and Git. No credentials are needed in Phase 0.

Runtime modes are exactly `DRY_RUN`, `STAGING`, and `LIVE`. Log levels are `DEBUG`,
`INFO`, `WARNING`, `ERROR`, and `CRITICAL`. Reading a configured mode does not start
a runtime. Phase 0 exposes only `jhm version` and `jhm config`; no `run` command or
external executor exists. A future LIVE runner must require both configured LIVE
and explicit CLI authorization, with separate approval for irreversible actions.

```bash
uv run --locked jhm --help
uv run --locked jhm config --runtime-file config/runtime.yaml
uv run --locked python -m job_hunting_machine version
```

## Foundation interfaces

The package uses a `src` layout under `src/job_hunting_machine`:

| Module | Responsibility |
| --- | --- |
| `config.py`, `runtime.py` | Immutable Pydantic settings and runtime-mode enum |
| `security/paths.py` | Fixed project boundary and guarded local writes |
| `clock.py` | Injectable UTC clock and millisecond ISO-8601 formatting |
| `ids.py` | Central ULID and architecture-prefixed ID generation |
| `observability/logging.py` | Structured JSON logging to stderr |
| `cli.py` | Read-only configuration and version commands |

SQLAlchemy and Alembic are installed and locked. Database engines, tables, migration
scripts, transactions, audit persistence, and durable ID handling belong to Phase 1.
There is no database creation or schema mutation in Phase 0.

### File writes

Every application file write must use `PathGuard`. Its boundary cannot be widened by
configuration. Tests may select a narrower directory inside the real project root.

```python
from job_hunting_machine.security.paths import PathGuard

guard = PathGuard()
guard.mkdir("reports", exist_ok=True)
guard.write_text("reports/example.txt", "Local artifact\n")
```

Parent traversal, outside absolute paths, and symbolic-link write paths are rejected.
The guarded write helpers use descriptor-relative, no-follow operations and atomic
replacement. New directories are private (`0700`); files are private (`0600`).
`validate_write()` is a preflight check, not a safe substitute for a guarded write:
do not validate a path and then write it with an unguarded third-party API.

This is an application safety boundary, not an operating-system sandbox. It cannot
constrain arbitrary Python, shell commands, or a hostile process moving already-open
directories outside the root. The project and its ancestors must remain trusted.
Later integrations must preserve this boundary when handing paths to external libraries.
The helpers fsync file contents, but do not fsync parent directory entries; atomic
replacement alone does not guarantee that a new filename survives a power failure.

### Time, IDs, and logs

Inject `Clock` into code that needs time. `SystemClock` supplies UTC time;
`FrozenClock` makes tests reproducible. `format_utc()` emits timestamps such as
`2026-09-05T08:23:31.123Z` and rejects naive datetimes.

`IdGenerator` creates ULIDs from a 48-bit millisecond timestamp and 80 bits of
cryptographic randomness. The 128-bit value is encoded as 26 Crockford Base32
characters. Task IDs match `^TASK_[0-9A-HJKMNP-TV-Z]{26}$`; Application IDs use
the same suffix with `APP_`. The other architecture prefixes are `JOB_`, `APR_`,
`ART_`, `CNT_`, and `EVT_`. Generate an ID once at its future persistence boundary;
do not regenerate it when replaying work. Phase 0 does not persist or execute tasks.

JSON logs include timestamp, level, Task ID, Application ID, agent, event, status,
duration, and error class. Use static event names and approved correlation fields.
Do not log candidate content, passwords, cookies, tokens, or arbitrary exception
messages. The logger filters fields; callers still must avoid placing sensitive
values in allowed fields. Persistent log files and database audit events are deferred.

## Validation

From the project root with the setup above:

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
```

Pytest temporary files stay in `.pytest-tmp`; lint/type caches also stay in the
repository. Tests block outbound socket connections and DNS lookup, use synthetic
data, and exercise path confinement, configuration precedence, DRY_RUN defaults,
ID formats, deterministic clocks, structured logs, and the inspection CLI.

See [the Phase 0 implementation report](docs/phase-reports/phase0-report.md) for
the executed commands, acceptance results, and limitations. Existing candidate
documents remain untouched and ignored by Git. Next phase has not started.
