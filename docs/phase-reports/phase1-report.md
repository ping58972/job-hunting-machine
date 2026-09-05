# Phase 1 implementation report

Date: 2026-09-05  
Project: `/Users/ping58972/Documents/job-hunting-machine`  
Contract: Architecture v2, Phase 1 (section 84)  
Status: **Implemented; all acceptance checks passed**

## Summary

Implemented the SQLite persistence layer: the exact 23 Architecture v2 domain
tables, typed SQLAlchemy mappings, a frozen Alembic migration, explicit transactions,
audited repositories, durable IDs, optimistic Task versions, and qualification/salary
policy data. The default `data/job-hunting.db` is initialized and remains in DRY_RUN
configuration. There is no workflow runner or external integration.

AGENTS.md, Architecture v2, and the Phase 0 report were read before implementation.
Architecture v2 and the historical Phase 0 report were not changed. Work began from
the user's Phase 0 commit `d40624e`; no commit or remote operation was performed.

## Files created

```text
alembic.ini
docs/database.md
docs/phase-reports/phase1-report.md
src/job_hunting_machine/database/__init__.py
src/job_hunting_machine/database/engine.py
src/job_hunting_machine/database/models.py
src/job_hunting_machine/database/schema_types.py
src/job_hunting_machine/database/seeds.py
src/job_hunting_machine/database/migrations/__init__.py
src/job_hunting_machine/database/migrations/env.py
src/job_hunting_machine/database/migrations/script.py.mako
src/job_hunting_machine/database/migrations/versions/0001_architecture_v2.py
src/job_hunting_machine/database/repositories/__init__.py
src/job_hunting_machine/database/repositories/base.py
src/job_hunting_machine/database/repositories/activity.py
src/job_hunting_machine/database/repositories/jobs.py
src/job_hunting_machine/database/repositories/tasks.py
src/job_hunting_machine/database/repositories/applications.py
src/job_hunting_machine/database/repositories/artifacts.py
src/job_hunting_machine/database/repositories/approvals.py
tests/integration/test_database.py
tests/integration/test_database_cli.py
tests/integration/test_repositories.py
tests/integration/test_schema.py
tests/integration/test_seeds.py
```

Generated local state: `data/job-hunting.db`, its transient SQLite sidecars, contained
test databases/caches, and rebuilt wheel/source distribution in `dist/`. These remain
ignored by Git. No candidate document was modified.

## Files modified

```text
.env.example
AGENTS.md
README.md
config/logging.yaml
config/runtime.yaml
pyproject.toml
src/job_hunting_machine/__init__.py
src/job_hunting_machine/cli.py
src/job_hunting_machine/ids.py
src/job_hunting_machine/security/paths.py
tests/conftest.py
tests/unit/test_cli.py
tests/unit/test_ids.py
tests/unit/test_paths.py
```

Changes extend the scope documentation, add explicit local `jhm db init`, validate
persisted IDs centrally, reserve private database files without replacing their
inodes, include Alembic configuration in source distributions, and extend tests.
Dependency versions and `uv.lock` are unchanged; existing dependencies were sufficient.

## Migration and schema

Revision: **`0001_architecture_v2`**; parent: none.

- Creates all **23 domain tables**, the **2 specified queue indexes**, and **3 audit
  protection triggers**. Alembic adds its own version table, giving **24 tables total**.
- Preserves architecture columns, types, defaults, JSON checks, allowed-value checks,
  unique constraints, foreign keys, and cascade actions. An independent test compares
  migrated SQL with all 23 CREATE TABLE definitions extracted from Architecture v2.
- Explicit `NOT NULL` primary keys enforce the architecture's required durable identity;
  SQLite otherwise permits NULL for a bare `TEXT PRIMARY KEY`.
- The migration contains frozen SQL rather than importing live ORM metadata for DDL.
  Foreign-key clauses are formatted so SQLite/SQLAlchemy reflection retains CASCADE
  actions. ORM/migration comparison reports no differences.
- The default database was migrated only with Alembic. No `create_all()` or manual
  production schema mutation was used.
- **No LangGraph checkpoint database or checkpoint tables were created.** The
  application engine rejects the reserved checkpoint filename.

## Architecture decisions

1. **Explicit transaction ownership.** `Database.transaction()` commits on success and
   rolls back on exceptions. Repositories flush but never commit. Compound operations
   use savepoints so a caught inner error also leaves no partial operation behind.
   An outer rollback undoes previously released repository savepoints.
2. **Connection invariants.** Each native SQLite connection enables `foreign_keys=ON`,
   `journal_mode=WAL`, `synchronous=NORMAL`, and `busy_timeout=5000`. `temp_store=MEMORY`
   keeps SQLite temporary query data off external temporary directories. Explicit
   BEGIN includes DDL and savepoints in transactions. NullPool releases connections
   at their ownership boundary.
3. **File confinement.** PathGuard reserves a regular database file without truncation,
   with mode `0600`; new directories are private. The engine revalidates the database
   and `-wal`, `-shm`, and `-journal` paths at connection, statement, and transaction
   boundaries. A native authorizer blocks ATTACH, VACUUM INTO, extension-loading SQL,
   and changes that disable required PRAGMAs.
4. **Atomic Application creation.** A persisted PASSED job produces one pipeline row,
   one details row, and one READY `BUILD_RESUME` task with its audit events, together.
   The application remains `QUALIFIED`, at stage `RESUME`; creating metadata does not
   execute a worker. The supplied job URL must match the persisted job identity.
5. **Durable IDs and replay.** Central validation enforces known prefixes and canonical
   ULIDs. ORM types never generate IDs implicitly. Replaying identical Job/Task/
   Application operations returns stored identities; Artifact/Approval replay uses
   the supplied stored ID. Conflicting identity payloads are rejected. Task dedupe
   keys and canonical URLs also have database uniqueness constraints.
6. **Optimistic concurrency.** Task status updates match the expected version, increment
   it by one, and append an old/new-state audit in the same transaction. A stale
   expected version changes nothing and creates no audit event. SQLAlchemy's version
   mapping also supports stale-write detection. Queue transition policy and execution
   remain later-phase work.
7. **Append-only audit.** The repository exposes append/read only. Triggers reject SQL
   UPDATE, DELETE, and INSERT replacement of an existing Event ID. Testing found that
   ordinary DELETE triggers alone did not prevent SQLite `INSERT OR REPLACE`; the
   third trigger closes that bypass and has a regression test.
8. **UTC storage.** Operational timestamps are canonical millisecond UTC strings,
   generated by the shared Clock. ORM types validate IDs/timestamps on writes and
   reads. Date-only policy/job fields remain TEXT as specified by the architecture.
9. **Policy data only.** Seeding inserts missing versioned data, preserves edited or
   disabled rows, and adds one `POLICY_SEEDED` event only when it inserts rows. Policy
   identities use plain ULIDs where the architecture defines no prefix, and subsequent
   runs find existing policy identities. No candidate fact is inferred from policy dates.

## Seeded policy and actual default database

The default database contains:

| Record group | Count |
| --- | ---: |
| Qualification rule sets | 1 |
| Qualification rules | 13 |
| Salary location rules | 3 |
| Administrative POLICY_SEEDED audit events | 1 |
| Jobs, Applications, Tasks, candidate facts, external actions | 0 each |

Rules cover January–August 2027 internship overlap, USA-only internships, paid/CPT
requirements, relevant technical fields, location salary thresholds, open applications,
new-graduate/early-career timing and the exact country list, the five-year minimum
experience exclusion, and evidence-aware U.S. full-time OPT/sponsorship distinctions.
Unknown sponsorship language remains UNKNOWN and requires review. This is stored
policy data, not a running evaluator or verified candidate profile.

Salary policy: **USD 20/hour USA fallback; USD 35/hour San Francisco and New York City**.
ISO country codes represent the architecture's country names. A repeated default
initialization inserted **0 rule sets, 0 rules, and 0 salary rows** and no duplicate audit.

Default database checks: schema at head; 24 tables; `PRAGMA integrity_check = ok`;
`PRAGMA foreign_key_check` returned no violations; database permissions **0600**.
`jhm config` reports **DRY_RUN**, phase 1, and `workflow_available: false`.

## Commands run

All Python/tool commands used the cached local environment with `--offline` and
`--locked`; no dependency downloads or external network requests occurred.

```bash
cd /Users/ping58972/Documents/job-hunting-machine
export TMPDIR="$PWD/.tmp"
uv run --offline --locked ruff format .
uv run --offline --locked pytest
uv run --offline --locked ruff check .
uv run --offline --locked ruff format --check .
uv run --offline --locked mypy
git diff --check
uv run --offline --locked jhm db init
uv run --offline --locked jhm db init
uv run --offline --locked alembic current
uv run --offline --locked alembic check
uv run --offline --locked jhm config
uv build --offline
```

Additional checks used targeted pytest runs with project-local basetemps, local
SQLAlchemy/SQLite schema and integrity inspection, package archive inspection,
Git status/ignore checks, and an isolated audit-replacement reproduction. Initial
validation issues were fixed before the final run: lint formatting, SQLite's alternate
authorization error wording, audit replacement protection, and foreign-key reflection
across multiline SQL. The final results below are successful.

## Acceptance-test results

Final complete run: **154 passed, 0 failed, 0 skipped** on macOS, Python 3.12.14,
SQLite 3.53.4, pytest 9.1.1, and SQLAlchemy 2.0.52.

| Test group | Passed |
| --- | ---: |
| SQLite engine / persistence / safety | 24 |
| Database initialization CLI | 2 |
| Repositories / atomicity / durable IDs / audit | 22 |
| Schema / ORM parity / storage validation | 8 |
| Policy seeding | 2 |
| Foundation unit tests | 96 |
| **Total** | **154** |

| Required scenario | Result and evidence |
| --- | --- |
| Migration from empty DB | PASS: all architecture tables plus Alembic revision; exact schema and ORM parity |
| Foreign keys | PASS: every connection enables them; orphan task memory rejected |
| Duplicate URL protection | PASS: repository replay reuses ID; direct duplicate insert rejected |
| Duplicate task protection | PASS: dedupe replay preserves task; direct duplicate key rejected |
| Transaction rollback | PASS: flushed DML/DDL and released nested repository savepoints roll back |
| Passed job creates Application + Resume task atomically | PASS: pipeline, details, READY task, associations, and audits agree |
| Failed transaction creates no partial application | PASS: failure injected after the pipeline/details/task/task-audit writes leaves none behind |
| Activity events persist correctly | PASS: linked IDs, old/new states, UTC fields; update/delete/replace rejected |
| Restart preserves state | PASS: committed data and exact Application/Task IDs survive engine reopen |
| Optimistic Task version | PASS: stale version rejected without state or audit changes |
| Artifact/Approval identity | PASS: exact IDs persist; identical metadata replays; changed hashes rejected |
| Seed repeatability | PASS: edits, disabled rules, and IDs preserved; no duplicate data or seed event |

Other final checks:

- **Ruff:** all checks passed.
- **Ruff format:** 44 files already formatted.
- **mypy:** strict check passed, 39 source files checked.
- **Alembic:** `0001_architecture_v2 (head)`; no new upgrade operations detected.
- **Git diff whitespace:** passed.
- **Packaging:** source distribution and wheel build offline; package inspection
  excludes databases, candidate documents, credentials, and development caches and
  includes the migration resources and typing marker.

## External operations and AI usage

No network calls, OpenAI calls, Slack calls, browser calls, email sends, submissions,
or other external mutations. Tests block socket connections and DNS and use only
synthetic data in contained databases. Application AI/model usage is **0 calls / $0**;
coding-assistant usage is separate and is not measured by the application.

## Security concerns and known limitations

- Native SQLite file IO requires trusted project directories. PathGuard and the
  authorizer do not provide a custom VFS or an OS sandbox against hostile concurrent
  directory/mount changes or arbitrary Python/native connections bypassing this engine.
- WAL/NORMAL follows the architecture; full power-loss durability is not promised.
  Future recovery/backup services are not implemented in Phase 1.
- Supported repositories audit their mutations. Raw SQL/direct ORM writes bypass
  repository audit creation and some ID/timestamp validation; they are not the normal
  application write API. Existing audit rows remain trigger-protected. Audit callers
  must not put secrets or unnecessarily sensitive values into metadata.
- Canonical URL uniqueness uses the supplied exact canonical string; URL discovery,
  normalization, fetching, and qualification evaluation remain Phase 5.
- Artifact/Approval repositories validate metadata/IDs, paths, and digest format.
  They do not generate files, verify actual content hashes, approve payloads, or perform
  external actions. Those operations belong to their explicitly requested later phases.
- SQLite busy/snapshot conflicts may still reach callers after the configured timeout;
  automatic workflow retry, leasing, and recovery remain Phase 2. No queue executor exists.
- Policy timestamps/dates are policy configuration, not confirmed candidate graduation
  or work-authorization facts. Seeding never creates candidate facts.
- Validation used macOS/Python 3.12; other supported Python/platform combinations were
  not exercised. The default database was never destructively downgraded.

**Next phase has not started. Phase 2 has not started.**
