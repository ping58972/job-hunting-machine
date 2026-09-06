# State machines

The durable queue and application transitions are defined in Architecture v2. Architecture
Amendment A1 changes the internal document-generation checkpoints without changing the surrounding
application states.

```text
QUALIFIED / BUILD_RESUME
  -> TEMPLATE_VALIDATED
  -> FACTS_RETRIEVED
  -> PROJECTS_SELECTED
  -> SKILLS_SELECTED
  -> TEX_GENERATED
  -> TEX_SAVED
  -> PDF_COMPILED
  -> PDF_VALIDATED
  -> ONE_PAGE_CONFIRMED
  -> ARTIFACTS_REGISTERED
  -> FORM_READY / FORM_PROCESS
```

When policy requires a cover letter, successful resume publication creates
`BUILD_COVER_LETTER`; `FORM_PROCESS` is created only after its TEX/PDF pair validates. Missing
facts, invalid markers, compiler errors, overflow exhaustion, template changes, and artifact hash
conflicts pause for human review. Unexpected worker failures do not advance application business
state. Recovery reuses matching immutable output and never overwrites an approved artifact version.

See [Durable orchestration](orchestration.md), [LaTeX documents](resume-artifacts.md), and
[Architecture Amendment A1](architecture-amendments/A1-latex-document-pipeline.md).
