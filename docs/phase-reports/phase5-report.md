# Phase 5 report: Retrieve Links and Qualification

Completed September 5, 2026 (America/New_York). Architecture v2 remains authoritative.
**Next phase has not started.** No application submission implementation was added.

## Summary

Implemented deterministic Slack URL intake first, then HTTP-first job evidence fetching,
JSON/JSON-LD and HTML parsing, optional read-only Playwright fallback, editable deterministic
qualification rules, and optional structured semantic checks through ModelGateway.

PASS publishes PASSED + Application + Application Details + READY BUILD_RESUME + audits
in one transaction. FAIL publishes ABORTED with failed rule IDs and explanations.
Unknown evidence publishes NEEDS_REVIEW. Access challenges additionally persist a human
interrupt. Worker failure alone does not assign an application rejection.

## Files created

- `src/job_hunting_machine/agents/__init__.py`
- `src/job_hunting_machine/agents/retrieve_links.py`
- `src/job_hunting_machine/agents/extraction.py`
- `src/job_hunting_machine/agents/fetch.py`
- `src/job_hunting_machine/agents/qualification.py`
- `src/job_hunting_machine/agents/worker.py`
- `tests/integration/test_qualification.py`
- `docs/qualification.md`
- `docs/phase-reports/phase5-report.md`

## Files modified

`AGENTS.md`, `README.md`, `.env.example`, `pyproject.toml`, `uv.lock`,
`config/prompts.yaml`, `src/job_hunting_machine/cli.py`, and `tests/unit/test_cli.py`.
The CLI test now inspects configuration without assuming that a user's Slack allowlist
is empty. Existing phase reports remain historical evidence.

## Database migrations

None required. Existing Alembic revision `0001_architecture_v2` already owns the job,
queue, memory, application, rule, salary and audit tables. No production schema changes
were made. LangGraph continues to own its separate checkpoint database.

## Architecture decisions

1. Canonical URL uniqueness and durable task dedupe keys protect repeated/concurrent intake.
   Job IDs are generated only by the repository at insertion. Intake does not use AI.
2. Fetch and model operations occur outside replay-sensitive graph nodes. The graph reads
   persisted facts, policy and time inputs. Content-addressed snapshots preserve raw evidence,
   extraction, source attempts and exact rule definitions across restart.
3. Final publication uses the queue lease fence and one database transaction. Replaying
   after a published decision returns without generating new Application or Resume Task IDs.
4. Deterministic salary, country, date, paid-work, experience and open/deadline checks precede
   semantics. Unsupported operators and missing/ambiguous data require review. Known hard
   failures skip model calls. Publication rechecks time-sensitive rules after interruptions.
5. Model interpretation is limited to relevance, CPT and authorization, with exact evidence
   quotes and configured confidence thresholds. Known facts are not overwritten. Luna starts;
   Terra escalation and optional Sol remain controlled by the existing route/ceiling/budgets.
   Structured semantic findings and confidence are saved with the evidence snapshot.
6. A durable model-start marker avoids blindly billing an interrupted request again. Missing
   saved results retain unknown values for review; usage reservations remain conservative.
7. Live HTTP, browser and model transports each require explicit opt-in. The offline CLI
   processes intake only. Normal continuous operation has eight bounded workers with graceful
   shutdown. No BUILD_RESUME task is executed by this worker.
8. HTTP requests reject private destinations, credentials and LinkedIn, revalidate redirects,
   and pin validated DNS addresses while retaining Host/TLS SNI. Browser requests pass through
   this same GET-only transport; no authenticated storage, forms, clicks or submission exists.

## Validation and commands

Commands ran from the project root with `.tmp` present and `TMPDIR="$PWD/.tmp"`:

```bash
uv add 'httpx>=0.28,<1' 'beautifulsoup4>=4.13,<5' 'playwright>=1.50,<2'
uv add --dev 'types-beautifulsoup4>=4.12,<5'
uv run --offline --locked pytest tests/integration/test_qualification.py -x
uv run --offline --locked pytest
uv run --offline --locked pytest tests/integration/test_qualification.py -q
uv run --offline --locked ruff check .
uv run --offline --locked ruff format .
uv run --offline --locked ruff format --check .
uv run --offline --locked mypy
uv run --offline --locked jhm qualify --help
uv run --offline --locked jhm config
git diff --check
```

- Full suite: **284 passed**, zero failures/skips, 10.13 seconds.
- Phase 5 acceptance suite: **35 passed**, zero failures/skips.
- Ruff lint: passed. Formatting: **85 files already formatted**.
- Strict mypy: passed, **71 source files**.
- CLI inspection confirmed Phase 5 capability, DRY_RUN and disabled submission capability.
- Whitespace check passed.

Development tests found a structured experience requirement being shadowed by the description;
normalization now considers both sources. Review also caught the New York/New York City salary
alias issue; a regression test confirms that the higher configured threshold applies.

## Acceptance scenarios

| Scenario | Result | Evidence |
| --- | --- | --- |
| Valid paid US 2027 internship with CPT evidence | PASS | PASSED and exactly one Application/Details/READY Resume task |
| Unpaid internship | PASS | ABORTED, paid rule ID and explanation |
| Internship outside USA | PASS | ABORTED, country rule |
| $18/hour standard-cost city | PASS | Fails $20 policy minimum |
| $40/hour high-cost city | PASS | Passes $35 policy minimum |
| 5+ years required | PASS | Minimum 5 fails |
| 3–5 years required | PASS | Minimum 3 passes |
| Explicit no sponsorship, US full-time | PASS | ABORTED with authorization rule |
| Sponsorship absent | PASS | NEEDS_REVIEW; no invented support |
| Expired deadline | PASS | ABORTED; explicit time-boundary regression |
| Duplicate URL | PASS | One NEW Job ID and one qualification task |
| Removed page | PASS | Raw evidence retained; ABORTED |
| Browser fallback | PASS | Fake HTTP shell followed by rendered fixture; both snapshots retained |
| Crash/resume after saved evidence | PASS | Expired lease recovered; no refetch |
| Crash after decision publication | PASS | No duplicate application or decision audit |
| Atomic failure during application creation | PASS | No partial application/details/task or PASSED state |
| Luna-to-Terra semantic escalation | PASS | Mocked low-confidence then supported quoted finding |
| Invented sponsorship from unrelated quote | PASS | Remains UNKNOWN/REVIEW |
| HTTP transport safety | PASS | Mock verifies IP pinning/Host/SNI and private redirect rejection |
| Challenge handling | PASS | Review and no browser bypass; interrupt uses existing durable queue |

## External operations, money and security

Dependencies were downloaded and official Schema.org/Playwright documentation was read.
No live job page was fetched; no browser binary was installed/launched; no Slack or OpenAI
request, employer message, or application submission was performed. Live AI spend: **$0**.
Model usage in tests is synthetic and persisted to isolated fixture databases. Normal tests
block outbound sockets and DNS.

Generated evidence uses PathGuard and remains under the project root, outside Git. Browser
requests are unauthenticated public reads. Evidence may contain sensitive content; keep local
directories trusted. Do not supply credentials in job URLs. GET-only does not guarantee that
an arbitrary external site implements GET without effects.

## Known limitations

- Real Chromium rendering, employer ATS pages, workspace permissions and live model behavior
  were not tested. Browser fallback acceptance uses a deterministic fake adapter.
- Generic parsing is deliberately conservative. Vague seasons, unfamiliar metro aliases,
  conditional requirements and complex ATS metadata may require review. No complete ATS adapter
  catalog, foreign exchange conversion, or invented annual-to-hourly conversion is included.
- Unknown authorization uses bounded semantic interpretation or human review, not an external
  company-policy research crawler. Full review resolution belongs to later work.
- Exactly one enabled policy set is required; operators manage activation explicitly. Existing
  snapshots retain policy definitions after edits. There is no policy administration UI.
- A read can repeat if interrupted before its snapshot commit. Unreferenced content-addressed
  files may remain after interruption. Evidence is decoded HTML, not compressed response bytes.
- Interrupted semantic results are conservatively reviewed rather than automatically rebilled.
  Exhausted/fatal worker failures require operator investigation; job business state remains ACTIVE.
- Per-domain throttling and a production-wide operational dashboard are not implemented.

**Next phase has not started.**
