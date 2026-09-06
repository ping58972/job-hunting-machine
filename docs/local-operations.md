# Local Operations and Startup

Phase 12 provides local operator controls for the authoritative SQLite database. Run commands
from the fixed project root with Python 3.12 and the locked `uv` environment:

```bash
cd /Users/ping58972/Documents/job-hunting-machine
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv run --locked jhm db init
uv run --locked jhm doctor
```

All commands below remain local. None changes `JHM_RUNTIME_MODE`, enables a provider, sends an
email, submits an application, or bypasses a provider-specific opt-in.

## Operator commands

```bash
uv run --locked jhm status
uv run --locked jhm queue
uv run --locked jhm applications --limit 100
uv run --locked jhm costs
uv run --locked jhm db integrity
uv run --locked jhm doctor
```

`status` gives a compact workload and health view. `queue` groups tasks by state and type and
counts expired ACTIVE leases and due retries. `applications` is a bounded status view. `costs`
separates settled estimates from unresolved model reservations. `db integrity` checks SQLite,
foreign keys, Alembic revision, WAL, and busy timeout. `doctor` adds artifacts, approvals,
candidate provenance, browser state, architecture boundaries, and versioned eval datasets.

Doctor also validates the authoritative LaTeX resume markers, the root-confined build directory,
`pypdf`, and an installed `latexmk`, `pdflatex`, or `tectonic`. It reports a failure when no
supported compiler exists and never installs one. Run the narrower check with
`uv run --locked jhm resume check`.

A warning is actionable but does not make the command fail. A failed doctor invariant exits with
status 2. The `safe_to_enable_live` field remains false because local static checks cannot verify
real credentials, account scopes, provider behavior, or a real end-to-end approved operation.

## Backups

Create a SQLite online backup while the application database may be open:

```bash
uv run --locked jhm backup
```

Each run creates a unique private directory under `backups/`. It uses SQLite's backup API for
`data/job-hunting.db` and, when present, `data/langgraph-checkpoints.db`. The backup includes
configuration, Architecture v2, lock and build metadata, and a canonical manifest with a SHA-256
and byte count for every file. The command verifies each copied database with
`PRAGMA integrity_check`. Backups are Git-ignored and never leave the project root.

Architecture v2 recommends retaining 30 daily backups. Phase 12 records that recommendation in
the manifest and leaves deletion to the operator, so a cleanup defect cannot remove the only
known-good backup. Periodically restore a backup to an isolated root-local database and run:

```bash
uv run --locked jhm db integrity --database .tmp/restore-check.db
```

## Recovery after interruption

After a laptop shutdown, process kill, or daemon restart, run:

```bash
uv run --locked jhm recover
uv run --locked jhm queue
uv run --locked jhm doctor
```

Recovery first requires a clean, compatible application schema. It then returns only expired
queue leases to eligible work. `WAITING_HUMAN` remains unchanged.
Abandoned browser, Slack, submission, and Gmail ledger rows become recoverable or
`UNKNOWN_RESULT` according to their existing action contracts. Recovery uses fake adapters and
DRY_RUN executor instances; it never repeats an uncertain external action. Submission and email
unknown results require their dedicated reconciliation paths.

Worker startup should follow this order:

1. initialize or migrate the databases explicitly;
2. run `jhm recover` once from a single coordinator;
3. run `jhm doctor` and stop on a failure;
4. start only the required workers;
5. schedule due monitor tasks;
6. inspect `jhm status` after startup.

Do not run competing recovery coordinators. Queue lease fencing protects workers, but a single
startup owner makes operator intent and logs easier to audit.

## launchd on macOS

The examples in [`docs/launchd`](launchd/) run recovery once at login and enqueue due monitor
tasks hourly. They deliberately contain no `--live` flag and no provider environment flags.
Copy them into `~/Library/LaunchAgents/` only after reviewing the paths and log destinations:

```bash
cp docs/launchd/com.job-hunting-machine.recover.plist.example \
  ~/Library/LaunchAgents/com.job-hunting-machine.recover.plist
cp docs/launchd/com.job-hunting-machine.monitor-schedule.plist.example \
  ~/Library/LaunchAgents/com.job-hunting-machine.monitor-schedule.plist
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.job-hunting-machine.recover.plist"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.job-hunting-machine.monitor-schedule.plist"
```

Use `launchctl bootout` with the same paths to unload them. The plists use `/bin/zsh -lc` so the
locked `uv` command runs from the fixed project root. Logs stay under the root-local `logs/`
directory. Continuous workers should be added only for a specific offline or explicitly gated
transport after its command has been exercised manually; never put secrets or access tokens in a
plist.

## Shutdown and incident handling

Send SIGTERM or SIGINT and allow workers to checkpoint, stop claims, persist safe browser state,
release ownership, close browsers, and close databases. If the process cannot drain, terminate it,
then use the recovery sequence above after the lease expires. Do not manually edit queue leases or
application status rows. Preserve the database, checkpoint database, logs, action ledger, and the
newest backup when investigating an unknown external result.
