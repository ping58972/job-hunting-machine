# Phase 6 report: Candidate Knowledge Base and GitHub Project Catalog

Completed September 5, 2026 (America/New_York).
**Next phase has not started.** No resume editing or application submission was implemented.

## Summary

Implemented audited candidate-fact persistence, project/skill catalogs, exact source
provenance, explicit verification, read-only GitHub API transport, incremental repository
scanning, commit/blob tracking, README/source observations, skill evidence, change detection,
and verified-only retrieval. Optional AI can reorder an already filtered project shortlist
through ModelGateway; it cannot introduce projects, facts or metrics.

## Files created

- `src/job_hunting_machine/database/repositories/knowledge.py`
- `src/job_hunting_machine/knowledge/__init__.py`
- `src/job_hunting_machine/knowledge/evidence.py`
- `src/job_hunting_machine/knowledge/github.py`
- `src/job_hunting_machine/knowledge/extract.py`
- `src/job_hunting_machine/knowledge/scanner.py`
- `src/job_hunting_machine/knowledge/retrieval.py`
- `src/job_hunting_machine/knowledge/cli.py`
- `tests/integration/test_knowledge.py`
- `docs/candidate-knowledge.md`
- `docs/phase-reports/phase6-report.md`

## Files modified

`AGENTS.md`, `README.md`, `.env.example`, `pyproject.toml`, `config/models.yaml`,
`config/prompts.yaml`, `src/job_hunting_machine/cli.py`,
`src/job_hunting_machine/database/repositories/__init__.py`, and `tests/unit/test_cli.py`.
No additional dependencies were required.

## Database migrations

None. Existing Alembic revision `0001_architecture_v2` owns candidate_facts, project_catalog,
skill_catalog, queue, task memory and audit tables. No production schema was manually changed.
Fact/project/skill IDs use centralized plain ULIDs, as these tables have no architecture-defined
special prefix. Scans require persisted Task IDs and use the existing lease/retry contracts.

## Architecture decisions

1. All automatically extracted facts begin UNVERIFIED. Source appearance does not establish
   candidate authorship, contribution, skill proficiency, or a measured result. README numbers
   remain explicitly attributed quotations until human review; no metrics are generated.
2. Provenance includes the source reference, local path and SHA-256, exact line range/quote,
   and project/commit/source path when applicable. Insertions and verification validate evidence.
   Reusing a fact/source identity with changed content is rejected.
3. Verification uses an expected-status compare-and-set update and an audit. The CLI additionally
   requires the hash of the exact inspected value. Project and skill verification caches derive
   from authoritative current verified facts; they cannot grant verification independently.
4. GitHub transport has a fixed API origin, GET-only operations, bounded responses, no redirects,
   explicit live opt-in and no credential logging. Inventory pagination is bounded. Scans pin
   the commit/tree/blob identities rather than fetching mutable branch contents repeatedly.
5. Unchanged commits/metadata skip source downloads. Changed commits reuse validated unchanged
   blobs. A durable manifest records scope and added/modified/removed selected paths.
6. Publication atomically updates the project, proposed facts, derived skills and audit history
   under the task lease. Truncated/corrupt fetches cannot advance the current commit. A stale
   scan cannot overwrite a concurrently published snapshot. Commit-before-task-completion replay
   is idempotent. The scanner's durable effect boundary uses task memory, not graph-node effects.
7. New commits invalidate prior verified project facts conservatively. Retrieval independently
   rechecks the source commit and evidence hash, excluding stale, rejected, unverified or corrupt
   facts even if a catalog cache is stale.
8. Deterministic token matching runs before optional AI ranking. Only matching verified facts
   enter the model context. The model must return a permutation of supplied project IDs; invalid
   output falls back to deterministic order. Verification is checked again after the model await.
9. The existing project_matching route supplies the model selection. RANK_PROJECTS has a $0.05
   default task budget; application/global budgets and prompt/usage persistence remain centralized.

## Commands and results

Executed from the project root using the project-local temporary directory and uv cache:

```bash
uv run --offline --locked pytest tests/integration/test_knowledge.py -x
uv run --offline --locked pytest tests/integration/test_knowledge.py -q
uv run --offline --locked pytest
uv run --offline --locked ruff check .
uv run --offline --locked ruff format src tests
uv run --offline --locked ruff format --check .
uv run --offline --locked mypy
uv run --offline --locked jhm catalog --help
uv run --offline --locked jhm catalog worker
uv run --offline --locked jhm config
git diff --check
```

- Full suite: **307 passed**, zero failures/skips, **13.08 seconds**.
- Phase 6 integration suite: **22 passed**; one additional offline CLI test.
- Ruff lint: passed after correcting formatting/style findings.
- Ruff format: passed, **95 files already formatted**.
- Strict mypy: passed, **80 source files**.
- CLI smoke checks: catalog commands available, live GitHub disabled, DRY_RUN preserved.
- Whitespace validation: passed.

## Acceptance evidence

| Scenario | Result |
| --- | --- |
| Commit-bound README/source provenance and verified local hashes | PASS |
| Extracted metrics remain quotations; no invented measurements | PASS |
| New observations cannot enter resume retrieval | PASS |
| Explicit verification enables matching facts, projects and skills | PASS |
| Unchanged commit skips tree/blob downloads | PASS |
| New commit reuses unchanged blobs and invalidates prior verification | PASS |
| Deleted source invalidates derived skills | PASS |
| Truncated tree does not advance the catalog | PASS |
| Corrupt Git blob cannot publish | PASS |
| Fact insertion dedupe, conflicting replay and rejection | PASS |
| Tampered local evidence is excluded despite stored VERIFIED status | PASS |
| Bounded paginated inventory deduplicates repositories | PASS |
| Live GitHub requires explicit opt-in | PASS |
| Crash before/after publication resumes from saved snapshot without refetch/duplicates | PASS |
| Stale scan cannot overwrite a new commit | PASS |
| Injected fact failure rolls back project/facts/skills together | PASS |
| Model ranking rejects invented IDs and excludes irrelevant metric text | PASS |
| Revocation during model ranking removes facts before returning context | PASS |
| HTTP adapter uses GET at the fixed origin and refuses redirects | PASS |
| Stale verification, missing quote and wrong review hash are rejected | PASS |
| Rate limit is classified retryable | PASS |

## External operations, AI usage and security

Official GitHub documentation was read. All repository API operations used synthetic fixture
or HTTP mock transports. No live GitHub repository scan, OpenAI call, Slack message, candidate
fact verification, resume edit or application submission was performed. **Live AI spend: $0**.
Model usage records created by tests contain synthetic usage in isolated test databases.
Normal tests block outbound sockets and DNS.

Evidence remains beneath the project root through PathGuard and outside Git. Credentials are
process-only and excluded from logs. Sources are parsed as data; repository code is not executed.
Local directories must remain trusted. Verification is an explicit local operator action with an
audited reviewer label, not a remote authentication system.

## Known limitations

- Live GitHub availability, authorization and rate-limit timing were not tested. Public inventory
  and explicitly selected repositories are supported; rename/unavailable-repository reconciliation
  requires operator attention.
- Scans are deliberately bounded: up to 40 selected regular UTF-8 files, 200 KB each. Truncated
  API trees fail. Unsupported files, symlinks, submodules and hidden/vendor paths are excluded.
  The manifest records coverage; path-change reports describe the selected scope.
- Skill extraction uses source extensions, Python AST imports and a small dependency-name map.
  It is not exhaustive and does not prove candidate proficiency or contribution.
- Every changed commit requires fresh verification, even for unchanged source observations.
  Historical facts stay available for audit. Orphan immutable evidence may remain after a crash.
- Retrieval uses local scans and simple deterministic term matching, not a semantic index. The
  initial facts CLI lists 200 records; larger catalogs can use the repository API.
- Catalog identity consistency relies on the lease/transaction service contract; arbitrary direct
  ORM writes bypass application-level protections. Future resume creation must revalidate fact IDs
  at consumption time; retrieval is not a permanent authorization.
- No automatic scheduler, candidate-contribution verifier, resume generation, or resume editing
  is included. See `docs/candidate-knowledge.md` for setup and API details.

**Next phase has not started.**
