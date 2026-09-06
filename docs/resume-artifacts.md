# Local LaTeX resume and cover-letter artifacts

Architecture Amendment A1 makes `source/NDanddank_resume.tex` the only authoritative resume
template. Normal processing reads this file and never modifies it. The ignored
`resumes/NDanddank_resume.tex` file is retained only as a legacy reference snapshot and is never a
runtime input.

## Marker contract

The resume template must contain each marker exactly once and in this order:

```latex
% JHM:PROJECTS:START
% JHM:PROJECTS:END
% JHM:SKILLS:START
% JHM:SKILLS:END
```

Only text between each matching pair is machine-owned. Regions may not overlap. Missing,
duplicated, reversed, or overlapping markers stop the task before a generated file is written.
Template-authored LaTeX outside those regions is copied byte-for-byte.

The cover-letter template is `source/NDanddank_cover_letter.tex` and uses one
`JHM:COVER_LETTER` marker pair. Plain-text values are escaped before insertion; template commands
are not globally escaped.

## Pipeline

```text
current VERIFIED facts and project evidence
  -> deterministic retrieval
  -> Terra selection and configured Sol finalization through ModelGateway
  -> marker-scoped LaTeX rendering
  -> immutable job-specific TEX
  -> LatexCompiler
  -> PDF validation and deterministic page count
  -> TEX/PDF artifact registration in one database transaction
  -> application-specific PDF selected by Form Agent
```

Generated paths include Application ID, Task ID, artifact version, and compression round. Their
human-readable base names are:

```text
NDanddank_resume_<Company>_<Position>_<MMDDYYYY>.tex
NDanddank_resume_<Company>_<Position>_<MMDDYYYY>.pdf
NDanddank_CoverLetter_<Company>_<Position>_<MMDDYYYY>.tex
NDanddank_CoverLetter_<Company>_<Position>_<MMDDYYYY>.pdf
```

`LatexCompiler` detects `latexmk`, then `pdflatex`, then `tectonic` unless configuration puts a
different supported backend first. It invokes an argument array with `shell=False`, a bounded
timeout, and an output directory under `data/latex-build/<task-id>/`. It never downloads a
compiler and never falls back to a cloud document service. `compilation.json` retains bounded
diagnostic metadata; auxiliary files stay in the ignored build directory.

The resume is accepted only when `pypdf` can parse a nonempty, unencrypted PDF with exactly one
page. Overflow drops the lowest-ranked verified content and recompiles, up to
`max_compression_rounds`. The loop never changes font size, geometry, margins, spacing, or global
style. Exhaustion pauses for review.

## Truth and recovery

The model may select only IDs from the deterministic pool of current VERIFIED candidate facts.
The worker rechecks those facts and the master template hash before publishing. It escapes
inserted plain text including `#`, `$`, `%`, `&`, `_`, braces, tildes, carets, and backslashes.

Durable state records these checkpoints:

```text
TEMPLATE_VALIDATED
FACTS_RETRIEVED
PROJECTS_SELECTED
SKILLS_SELECTED
TEX_GENERATED
TEX_SAVED
PDF_COMPILED
PDF_VALIDATED
ONE_PAGE_CONFIRMED
ARTIFACTS_REGISTERED
```

If a process dies after TEX or PDF creation, immutable paths and hashes let the recovered worker
reuse matching output. Conflicting bytes fail closed. Repeated generation uses a new artifact
version; older versions remain available for audit. `application_details` identifies the active
resume and cover-letter PDF.

## Operator commands

```bash
uv run --locked jhm resume check

export OPENAI_ALLOW_LIVE=1
export OPENAI_API_KEY='key-from-secure-storage'
uv run --locked jhm resume worker --live-models --once
```

The first command performs no generation or network access. The worker uses local LaTeX for
documents; the flag enables only the separately gated model gateway. DRY_RUN remains the global
default. No document command submits an application or sends a message.
