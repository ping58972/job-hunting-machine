# Architecture Amendment A1 — LaTeX Document Pipeline Migration Report

**Date:** 2026-09-06  
**Result:** Complete  
**Runtime mode during migration:** DRY_RUN

## 1. Migration summary

Resume and cover-letter generation now runs locally from machine-editable LaTeX templates. The
pipeline writes immutable, versioned TEX/PDF pairs, validates the PDF, requires exactly one resume
page, and publishes artifacts atomically before creating `FORM_PROCESS`. Google Docs, GDOC pointer,
Drive copy/edit/export, DOCX, and LibreOffice paths are absent from the current application-document
runtime.

## 2. Files created

- `docs/architecture-amendments/A1-latex-document-pipeline.md`
- `docs/phase-reports/latex-migration-report.md`
- `docs/operations.md`, `docs/safety.md`, `docs/state-machines.md`
- `source/NDanddank_cover_letter.tex`
- `source/NDanddank_resume.tex` (the user-supplied template is now tracked and contains marker
  boundaries)
- `src/job_hunting_machine/database/migrations/versions/0002_latex_artifacts.py`
- `src/job_hunting_machine/resume/errors.py`
- `src/job_hunting_machine/resume/latex.py`

## 3. Files modified

- `.env.example`, `.gitignore`, `AGENTS.md`, `README.md`, `pyproject.toml`
- `config/resume.yaml`
- `docs/architecture-v2.md`, `docs/architecture-invariant-review.md`, `docs/database.md`,
  `docs/form-preparation.md`, `docs/local-operations.md`, `docs/resume-artifacts.md`,
  `docs/submission.md`
- `src/job_hunting_machine/database/models.py`
- `src/job_hunting_machine/database/repositories/artifacts.py`
- `src/job_hunting_machine/reliability/doctor.py`
- `src/job_hunting_machine/resume/__init__.py`, `artifacts.py`, `cli.py`, `config.py`,
  `planning.py`, `worker.py`
- `src/job_hunting_machine/submission/review.py`
- `tests/fault_injection/test_phase12.py`
- `tests/integration/test_database.py`, `test_form_browser.py`, `test_resume.py`,
  `test_schema.py`, `test_submission.py`
- `tests/unit/test_cli.py`

## 4. Files removed

These modules existed solely for the prior resume/cover-letter Google document path:

- `src/job_hunting_machine/resume/actions.py`
- `src/job_hunting_machine/resume/documents.py`
- `src/job_hunting_machine/resume/fake.py`
- `src/job_hunting_machine/resume/native.py`
- `src/job_hunting_machine/resume/template.py`

## 5. Dependencies added

None. Runtime `pypdf` was already present. The local machine already provides `latexmk` and
`pdflatex`.

## 6. Dependencies removed

None from `pyproject.toml`: `python-docx` and `docxtpl` were not installed dependencies, and the
repository had no LibreOffice package dependency. Google/HTTP packages remain because Slack,
Gmail, monitoring, and other non-document integrations still use them.

## 7. Database migration

Alembic revision `0002_latex_artifacts` recreates the SQLite `artifacts` CHECK constraint with
`RESUME_TEX` and `COVER_LETTER_TEX`. It retains legacy `RESUME_DOCX` and `COVER_LETTER_DOCX` values
so historical rows remain readable. Application code rejects creation of new legacy DOCX rows.

The existing `data/job-hunting.db` was backed up locally and upgraded from
`0001_architecture_v2` to `0002_latex_artifacts`. `PRAGMA integrity_check` returned `ok`, and
`PRAGMA foreign_key_check` returned no rows. A test also upgrades a populated revision-0001
database and preserves its historical DOCX row.

## 8. Obsolete Google Docs code removed

Removed the live Google Docs/Drive adapter, local Google Docs fake, document-ID validation,
native tab/table traversal, batch edit request generation, Drive-copy action ledger, GDOC pointer
creation, template proposal flow, and Google-to-PDF export. The resume CLI no longer accepts a
Docs folder, token, or Docs opt-in. Repository-wide search finds no current resume/cover-letter
runtime reference to `.gdoc`, Google document APIs, or the Google document MIME type.

## 9. Google integrations intentionally retained

Gmail draft/send infrastructure, Gmail application monitoring, related OAuth/token configuration,
Slack, and GitHub remain intact. No all-Google removal was performed.

## 10. Resume template strategy

`source/NDanddank_resume.tex` is authoritative and is never written by normal generation. Exact,
non-overlapping `JHM:PROJECTS` and `JHM:SKILLS` marker pairs bound the only writable regions.
Missing, repeated, reversed, or overlapping markers fail before output. All LaTeX outside these
regions is copied unchanged. `resumes/NDanddank_resume.tex` remains ignored and preserved as a
legacy reference snapshot; runtime code never reads it.

## 11. Cover-letter template strategy

`source/NDanddank_cover_letter.tex` is a simple ATS-safe local template with one
`JHM:COVER_LETTER` marker region. The same verified project and skill pool constrains its claims.
The durable task snapshot retains plain text for form text fields, while file workflows register
`COVER_LETTER_TEX` and `COVER_LETTER_PDF`.

## 12. LaTeX compiler strategy

`LatexCompiler` detects `latexmk`, `pdflatex`, and `tectonic` in configured preference order. It
uses argument arrays with `shell=False`, a bounded timeout, captured/bounded process output, and a
root-confined build/output path. It writes `compilation.json` metadata and raises a clear review
error for missing compilers, timeouts, nonzero exits, or missing output. It downloads or installs
nothing and has no cloud fallback.

## 13. One-page validation strategy

`pypdf` opens the generated bytes in strict mode and rejects encrypted, empty, invalid, or
zero-page PDFs. Resume acceptance requires exactly one page. Overflow removes the lowest-ranked
verified content and recompiles within `max_compression_rounds`; font, geometry, margins, spacing,
and global template design remain unchanged. Exhaustion pauses safely without artifacts or a form
task.

## 14. Crash-recovery changes

Task memory snapshots record the policy, template hash, selection, artifact IDs/version,
compression round, file hashes, and named checkpoints. Deterministic paths allow recovery to reuse
matching TEX/PDF bytes after a process death. Conflicting immutable bytes, changed templates,
revoked facts, changed application pointers, or altered artifact hashes fail closed. Publication
uses one database transaction and preserves old versions.

## 15. Tests added or replaced

- master-template existence, uniqueness/order/non-overlap of markers, and master immutability;
- LaTeX escaping for C++, R&D, percentages, underscores, currency, hashes, braces, tildes,
  carets, and backslashes;
- real `latexmk` compile success and deterministic one-page validation;
- invalid LaTeX, timeout, argument-array/no-shell behavior, and project-root confinement;
- TEX/PDF generation, hash persistence, exact application correlation, verified-fact revocation,
  content compression, bounded overflow failure, and crash/restart reuse;
- local cover-letter TEX/PDF and plain-text generation;
- Form Agent PDF-only selection and rejection of a TEX pointer;
- review payload TEX/PDF hashes and revocation after either source or PDF mutation;
- empty and populated old-database migrations plus legacy DOCX read compatibility;
- default CLI remains offline and contains no cloud document path.

The complete suite continues to cover Retrieve Links, Qualification, queue/recovery,
ModelGateway, Slack, GitHub knowledge, Form Agent, approvals, submission, outreach/Gmail, and
application monitoring.

## 16. Test result

`uv run --locked pytest`: **394 passed, 0 failed** in 30.52 seconds.

## 17. Ruff result

`uv run --locked ruff check .`: passed.  
`uv run --locked ruff format --check .`: passed.

## 18. mypy result

`uv run --locked mypy`: passed with no issues in 146 source files.

## 19. Compiler prerequisites

Install at least one supported local compiler and make it discoverable on `PATH`: `latexmk`,
`pdflatex`, or `tectonic`. The checked machine selected `/Library/TeX/texbin/latexmk`. Run
`uv run --locked jhm resume check` or `uv run --locked jhm doctor` after environment changes. JHM
does not install TeX.

## 20. Known limitations

- The generic project renderer uses verified fact statements as project bullets; richer titles,
  links, and dates require structured verified project facts rather than inference.
- A locally trusted TeX distribution is an environment prerequisite. The code confines configured
  inputs and outputs and escapes inserted values; it is not an operating-system sandbox for a
  maliciously replaced compiler binary or deliberately hostile master template.
- Plain cover-letter text is durable in the generating task snapshot. A future, separately scoped
  amendment could add a first-class text-artifact type if cross-task querying becomes necessary.
- Historical DOCX rows remain readable but are deprecated and cannot be produced by current
  repositories.

## 21. Live application confirmation

No real job application was submitted. Submission tests used the fake ATS only.

## 22. External message confirmation

No email, Slack message, LinkedIn message, or other external message was sent.

## 23. Google Docs confirmation

Google Docs is no longer used for resume or cover-letter creation, copying, editing, formatting,
or PDF export. No external Google document was read, created, or modified during this migration.

## Commands run

```bash
command -v latexmk
command -v pdflatex
uv run --locked jhm resume check
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked jhm backup
uv run --locked jhm db init
sqlite3 data/job-hunting.db 'select version_num from alembic_version; pragma integrity_check; pragma foreign_key_check;'
uv run --locked jhm doctor
git diff --check
uv lock --check
```
