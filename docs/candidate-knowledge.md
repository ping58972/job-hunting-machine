# Candidate knowledge and GitHub project catalog

Phase 6 records evidence and retrieves verified facts. It does not edit resumes.
Repository contents are source observations, not proof of candidate ownership,
contribution, proficiency, or measured performance.

## Explicit workflow

Initialize the existing schema with `jhm db init`. Enqueue an explicitly chosen repository
or a bounded inventory of an owner's public repositories:

```bash
uv run --offline --locked jhm catalog scan OWNER/REPOSITORY
uv run --offline --locked jhm catalog inventory OWNER
```

These commands only persist tasks. `jhm catalog worker` alone reports that live access is
disabled. To perform read-only GitHub API calls, explicitly supply `GITHUB_ALLOW_LIVE=1`
and run `jhm catalog worker --live --once`, or omit `--once` for continuous processing.
An optional `GITHUB_TOKEN` is read only from the process environment. Use least-privilege
read access. Inventory uses the public owner endpoint; private repositories can be named
explicitly when the supplied token has authorized read access. No token is needed in tests.
No cron job or unattended scan is installed automatically.

Each scan/inventory executes under a persisted Task ID and worker lease, with heartbeats,
bounded queue retries and graceful shutdown. A saved manifest makes publication restart-safe.
This scanner uses task memory for its effect boundary; it adds no replay-sensitive graph node.

## What is saved

- `project_catalog`: canonical repository identity, current commit SHA, inventory metadata,
  languages/framework observations, scan time, and the local manifest path.
- `candidate_facts`: proposed statements with source type/reference, exact quote and lines,
  local SHA-256 evidence, project ID and commit SHA where applicable, and verification status.
- `skill_catalog`: canonical skill names and supporting fact IDs. Verification is derived
  from current verified facts, not from tags or an imported package name.
- `activity_log`: scan/change events, verification/rejection decisions, invalidations and
  catalog refreshes in the same transactions as their state changes.

Fact and project IDs use centralized plain ULIDs because Architecture v2 defines no special
prefix for those tables. Source blobs and manifests are content-addressed files beneath
`evidence/projects`, written through PathGuard. Source code is parsed as data, never executed.
The scanner verifies each downloaded Git blob identity and its local SHA-256.

README statements are copied as explicitly attributed observations. A metric such as
“95% accuracy” stays an UNVERIFIED quotation; the scanner never upgrades it to a candidate
accomplishment or manufactures benchmark results. Python imports and dependency declarations
produce proposed skill evidence, not automatic assertions of expertise.

## Incremental scans and change handling

The API reader uses GET only against `api.github.com`; redirects are not followed. Inventory
is paginated and bounded to ten pages. A scan resolves a default-branch commit and its tree,
then requests immutable blob SHAs. Unchanged heads skip tree/blob requests. Changed commits
reuse unchanged locally validated blobs and download changed selected files only.

Publication records added/modified/removed paths within the selected scan scope. It advances
the project commit only after the complete selected snapshot, facts, skills and audits commit.
A truncated tree fails without advancing state. Concurrent stale scans cannot overwrite a
newer published snapshot; they retry from a fresh comparison. A crash after publication does
not duplicate facts or projects.

Any new project commit invalidates previous verified project facts, including observations
from unchanged files. This conservative rule requires fresh review. Historical facts remain
available for audit; retrieval also checks commit identity and evidence integrity at read time.

## Human review

Inspect proposed facts and their exact value hashes locally:

```bash
uv run --offline --locked jhm catalog facts
```

Review the statement, source quote, attribution and evidence before recording a decision:

```bash
uv run --offline --locked jhm catalog decide FACT_ID VERIFIED --value-sha256 HASH --reviewer REVIEWER
```

`REJECTED` and `UNVERIFIED` are also valid decisions. Use `--expected VERIFIED` when changing
an already verified fact. The CLI checks the exact inspected value hash; the repository
checks expected status, current project commit and evidence integrity. Reviewer names are
local audit labels, not a remote identity/authentication system.

All extracted facts start UNVERIFIED. The implementation never verified a real candidate
fact during development. Verification should confirm what the candidate can truthfully claim,
not merely that text appears in someone else's repository. There is no bulk auto-approval.

For local candidate sources, `CandidateFactRepository.add` accepts a proposed statement and
mandatory `Provenance` with an existing local evidence file, SHA-256, quote and line range.
The same audited review mechanism applies. Reusing a fact/source identity with different
content is rejected; record a new source version instead. Repository mutations belong inside
`Database.transaction(immediate=True)`; repositories never commit independently.

## Retrieval for later resume work

```bash
uv run --offline --locked jhm catalog retrieve "Python robotics machine learning"
```

The retrieval API first selects current VERIFIED facts with valid evidence, then matches
normalized job-requirement terms. Projects rank by distinct matched terms, with stable ID
ordering for ties. Results contain the exact fact IDs and provenance; unverified inventory
metadata does not enter the candidate context.

`ranked_retrieval(..., task_id=..., gateway=...)` optionally sends only that verified shortlist
to ModelGateway using the configured `project_matching` route. The model may reorder supplied
project IDs only. Unknown/duplicate IDs, budget errors or model failures fall back to the
verified deterministic result. Verification is checked again after the model await to handle
concurrent revocation. The `RANK_PROJECTS` task budget is $0.05 by default; existing application
and global budgets still apply. Supplying no gateway makes zero model calls.

A returned context reflects verification at retrieval time. Later artifact creation must
recheck fact IDs before use; no permanent approval is implied. Phase 7 has not started.

## Bounds and limitations

Scans select up to 40 regular UTF-8 source/README/manifest files, at most 200 KB per file;
README files sort first. Symlinks, submodules, hidden/vendor paths and unsupported formats
are excluded. Large or non-UTF-8 content needs a separate reviewed ingestion path. There is
no repository clone, code execution, notebook execution or PDF/document extractor here.
The manifest records eligible and selected counts, so partial coverage is visible. Reported
path changes refer to this selected scope, not necessarily the entire repository.

Metadata languages/frameworks are observations. Python imports and a small known-library
map drive skill extraction; arbitrary languages and frameworks are not exhaustively analyzed.
Renamed/deleted/unavailable repositories require operator attention; the service does not
silently follow a rename or remove historical evidence. API failures use bounded retries where
appropriate. Orphan immutable evidence files can remain after interrupted work.

The initial CLI lists 200 facts; larger catalogs can use the repository API. Retrieval loads
the local fact set and checks evidence rather than using a full-text index. There is no
cross-user access-control service or automated candidate-contribution verifier. Catalog tables
are reused as defined by Architecture v2, with writer reservations and service-level identity
checks; direct arbitrary ORM writes bypass those application-level contracts.

Tests use synthetic GitHub fixtures and HTTP mocks. Live GitHub behavior was not exercised.
The API shapes follow GitHub's [commit documentation](https://docs.github.com/en/rest/commits/commits)
and [Git tree documentation](https://docs.github.com/en/rest/git/trees).
