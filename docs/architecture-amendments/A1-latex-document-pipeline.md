# Architecture Amendment A1 — Local LaTeX Document Pipeline

**Status:** Adopted  
**Date:** 2026-09-06  
**Amends:** Architecture v2 application-document decisions

## Decision

Resume, cover-letter, and related application-document generation is local and root-confined.
The authoritative resume template is `source/NDanddank_resume.tex`; the authoritative
cover-letter template is `source/NDanddank_cover_letter.tex`. The runtime copies template content,
replaces only explicit machine-owned marker regions, saves a job-specific `.tex` artifact, and
compiles it directly to PDF through the centralized `LatexCompiler`.

The following original decisions are superseded:

- Google Docs/GDOC cloning, editing, and export for application documents;
- DOCX resume and cover-letter templates;
- `python-docx` or `docxtpl` generation;
- LibreOffice DOCX-to-PDF conversion.

The replacement decisions are:

- local LaTeX master templates;
- `RESUME_TEX` plus `RESUME_PDF` for generated resumes;
- `COVER_LETTER_TEX` plus `COVER_LETTER_PDF` for generated cover letters;
- direct local LaTeX-to-PDF compilation;
- `pypdf` validation and an exactly-one-page resume requirement;
- hash-bound, versioned artifacts selected through the exact Application ID.

Legacy `RESUME_DOCX` and `COVER_LETTER_DOCX` database values remain schema-readable so migration
does not destroy historical records. New repository writes reject them. Current workers cannot
create GDOC or DOCX application documents.

## Safety and recovery

`PathGuard` confines templates, generated outputs, build directories, compiler outputs, and
metadata to the fixed project root. Compiler invocation uses argument arrays, `shell=False`, and a
bounded timeout. No compiler is downloaded automatically and no cloud fallback exists.

Plain candidate, job, and model-selected values are escaped at insertion boundaries. Only current
VERIFIED candidate facts may enter generated claims. The resume worker records deterministic
checkpoints and revalidates source hashes, facts, TEX, PDF, page count, and database state before
publication. Old artifact versions remain immutable; application details point to the active PDF.

Submission approval binds the review payload to both TEX and PDF metadata and, critically, to the
exact PDF SHA-256 sent to an ATS. A changed or recompiled PDF changes the current review payload
and revokes the approval.

## Scope

This amendment removes Google document generation only. Gmail monitoring and drafting, Slack,
GitHub, and other unrelated integrations remain in place under their existing safety gates. It
does not authorize application submission, email sending, LinkedIn automation, or any LIVE mode.
