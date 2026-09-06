# Phase 7 report: Resume and Cover Letter

Completed September 6, 2026 (America/New_York).
**Stopped after Phase 7. Phase 8 form processing and submission are not implemented.**

## Result

Implemented native Google Docs resume copying, scoped PROJECTS/SKILLS changes,
verified project/skill retrieval, Terra planning, configurable Sol finalization,
cover-letter generation, deterministic PDF validation, content compression, hashed
artifact bundles, audited publication, and restart recovery. FORM_PROCESS is created
only after the configured resume/cover-letter artifacts validate successfully.

Final checks: **331 tests passed**, Ruff lint passed, Ruff formatting passed
(109 files), and strict mypy passed (92 source files). Phase 7 contributes 23
integration cases plus one offline CLI test; existing CLI expectations now identify Phase 7.

## Files created

- `src/job_hunting_machine/resume/__init__.py`
- `src/job_hunting_machine/resume/config.py`
- `src/job_hunting_machine/resume/native.py`
- `src/job_hunting_machine/resume/documents.py`
- `src/job_hunting_machine/resume/actions.py`
- `src/job_hunting_machine/resume/fake.py`
- `src/job_hunting_machine/resume/planning.py`
- `src/job_hunting_machine/resume/template.py`
- `src/job_hunting_machine/resume/artifacts.py`
- `src/job_hunting_machine/resume/worker.py`
- `src/job_hunting_machine/resume/cli.py`
- `config/resume.yaml`
- `tests/integration/test_resume.py`
- `docs/resume-artifacts.md`
- `docs/phase-reports/phase7-report.md`

## Files modified

`AGENTS.md`, `README.md`, `.env.example`, `pyproject.toml`, `uv.lock`,
`config/prompts.yaml`, `src/job_hunting_machine/cli.py`,
`src/job_hunting_machine/orchestration/worker.py`, and `tests/unit/test_cli.py`.

The user's pre-existing changes to `docs/architecture-v2.md`, including the GDOC
template choice, were preserved. This phase did not edit that architecture document.
No production database schema was manually changed. Existing Alembic tables cover
artifacts, candidate facts, external actions, task memory, queue, and audit events;
no new migration or manual LangGraph checkpoint table was necessary.

## Architecture decisions

1. **Native document copy.** The source `.gdoc` is a cloud pointer. Drive `files.copy`
   creates an independent native document; copying the pointer alone is insufficient.
   The supplied template was fetched read-only and its native structure inspected:
   one tab, a 19-row header table, and six PROJECTS content paragraphs. Local edit-plan
   validation found 25 editable paragraphs, including blank side cells/trailing
   paragraphs, all in the header. No DOCX reconstruction or browser conversion is used.
2. **Explicit edit boundary.** Recursive traversal covers tabs, headers, footers and
   tables. Scoped requests use tab/segment IDs, descending UTF-16 indexes, preserved
   paragraph newlines and original text styles. Old project dates/links and obsolete
   skill text are cleared inside the authorized sections. Protected content and links,
   document/paragraph/table styles, fonts and margins are checked on readback.
3. **Verified content only.** Deterministic Phase 6 retrieval filters current verified
   project facts and skills before model selection. Terra selects IDs; configured Sol
   finalization can only reorder or remove the plan's IDs. Rendering uses exact verified
   statements and canonical skill names. No free-form model claim enters an artifact.
   Facts, source hashes, and current project commits are checked again at publication.
4. **Retained template claims require verification too.** A template-proposal command
   saves native source evidence and creates an UNVERIFIED RESUME_TEMPLATE fact. An
   explicit audited human decision is required before configuring its fact ID. Source
   existence never grants verification. Protected content must still match that source.
5. **Central model infrastructure.** Existing resume_planning, resume_finalization and
   cover_letter routes, per-task/application/global budgets, structured validation,
   prompt hashes/cache keys and usage persistence remain owned by ModelGateway.
   A versioned resume_selection prompt constrains selection. A sealed pending request
   whose result was lost is not automatically repeated or its budget released.
6. **Durable effects.** ResumeWorker uses immutable content-addressed task-memory
   snapshots as effect boundaries, with existing Worker claim/heartbeat/recovery/
   shutdown handling. The base worker now exposes its claimed task-type tuple so an
   effect state machine need not register a dummy graph. No nondeterministic operation
   was placed inside replay-sensitive graph logic.
7. **External action ledger.** Native copy/create/edit passes through the document
   ExternalActionService using existing external_actions rows and audits. Copy intent
   is recorded before dispatch; an uncertain copy is reconciled through Drive
   appProperties rather than blindly repeated. Revision guards and desired-content
   readback handle repeated edits. Lease fencing protects ledger and local artifact writes.
8. **Bound one page by content.** Pypdf reads the real PDF page tree and validates
   expected text, including header content. The document revision/content is checked
   across export. Multi-page exports cause the lowest-ranked whole fact to be removed
   and rerendered, within the configured bound. Neither metrics nor qualifiers are
   truncated; font/margin shrinking is not implemented. Unsupported/clipped/invalid
   exports or exhausted compression require human review.
9. **Independent content hashes.** Each accepted bundle records the PDF, GDOC pointer,
   and native JSON snapshot with ART IDs and SHA-256. The pointer also binds the cloud
   document ID, revision, and snapshot hash. Because Architecture v2's artifact enum
   has no GDOC value, pointers and snapshots use OTHER with correct MIME types. No
   Google document is mislabeled DOCX. All artifacts remain unapproved for submission.
10. **Atomic handoff.** Artifact rows, application details, business status, next task,
    and audit are published in one transaction. Optional cover-letter policy creates
    BUILD_COVER_LETTER after resume validation and delays FORM_PROCESS until both
    documents validate. Final publication sets FORM_READY and creates READY FORM_PROCESS.
    Existing committed publication is recognized on restart without regressing the app.
11. **Naming and confinement.** Basenames follow the requested resume/CoverLetter
    Company_Position_MMDDYYYY convention, using the frozen UTC task date. Application,
    task and round directories avoid collisions. Every application write uses PathGuard.
    Candidate data, native source snapshots, generated bundles and credentials stay out
    of Git. ReportLab is a development dependency for genuine synthetic PDF fixtures;
    production PDF export comes from Google, with Pypdf as the page-count dependency.

## Acceptance results

| Scenario | Result |
| --- | --- |
| Native template structure, formatting and source preserved | Passed |
| PROJECTS and SKILLS change; obsolete claims/dates removed | Passed |
| Protected education/work text, hyperlinks and margins unchanged | Passed |
| Unknown fact IDs rejected; revoked facts cannot publish | Passed |
| Retained template requires explicit verified source review | Passed |
| Terra planning and Sol finalization routed/persisted correctly | Passed |
| Sol finalization can be disabled | Passed |
| PDF exists, expected text survives, exactly one page | Passed |
| Content compression: two pages to one without style changes | Passed |
| Resume and cover-letter basenames match required convention | Passed |
| GDOC, PDF and snapshot hashes recorded; artifacts unapproved | Passed |
| Crash after copy, edit, GDOC save or publication recovers | Passed, four crash points |
| Restart creates no duplicate artifacts or FORM_PROCESS task | Passed |
| Corrupt, clipped and irreducibly multi-page PDFs block handoff | Passed, three cases |
| Failure during artifact publication rolls back all DB output | Passed |
| Cover-letter policy holds FORM_PROCESS until both validate | Passed |
| Cover-letter compression does not duplicate the body | Passed |
| Human review/reverification can resume a waiting task | Passed |
| Unknown model outcome never repeats the billable call | Passed |
| External paths, traversal and symlink output escapes rejected | Passed |
| UTF-16 edit ranges target the correct header/tab only | Passed |
| Later-run font shrinking and protected-link changes detected | Passed |
| Google live opt-in and mocked HTTP revision/export contract | Passed |
| Default CLI performs no live call or form processing | Passed |

## Commands run

From `/Users/ping58972/Documents/job-hunting-machine`, with temporary files inside the root:

```bash
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv add 'pypdf>=6,<7'
uv add --dev 'reportlab>=4,<5'
uv run --locked pytest tests/integration/test_resume.py -x -q
uv run --locked pytest tests/integration/test_resume.py tests/unit/test_cli.py -x -q
uv run --locked ruff check . --fix
uv run --locked ruff format .
uv run --locked mypy
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked jhm resume worker --once
uv run --locked jhm resume --help
git diff --check
```

Also ran read-only source/template inspection and a local native edit-plan probe against
the fetched template snapshot. Iterative checks caught and fixed a task initially created
as NEW rather than READY, an outdated Phase 6 CLI assertion, and formatting/type issues.
Final full pytest run: **331 passed in 19.24 seconds**. Final static checks all passed.
Normal tests block sockets; no live OpenAI, Slack, browser, email or submission occurred.

## Known limitations

- No real application's Google document was copied, edited or exported during implementation.
  The live integration was checked with mocked HTTP contracts; the actual source was read-only.
  Native JSON fixtures and real synthetic PDFs validate control flow and parsing, but do not
  constitute visual proof of Google's renderer. Live generation revalidates native structure,
  revision, text and page count and fails closed when they do not match.
- GDOC is a mutable cloud document pointer. The local snapshot and PDF bind a validated
  revision; they do not lock future cloud edits. Later submission approval must use the
  selected immutable artifacts and any required revalidation.
- Preserved table geometry can make some content impossible to fit. Clipping, unsupported
  structures, unextractable fonts or exhausted compression requires review; no typography
  workaround is enabled. Fact wording must be useful and verified before selection.
- Model-output loss and uncertain copies with no unique reconciliation result require
  local review. A human retry does not approve facts or release unresolved reservations.
  Policy/selection changes to a sealed run require a fresh reconciled task.
- OAuth token refresh, automatic cloud sharing, permission changes, full visual/ATS scoring,
  form filling, submission and outreach are not implemented. Live generation remains explicitly
  opt-in and requires verified source configuration; the default command stays offline.

See [resume-artifacts.md](../resume-artifacts.md) for configuration, verification, execution,
recovery, output semantics, and official Google API references.
