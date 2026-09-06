# Phase 7: Resume and cover-letter artifacts

The worker copies the native Google document referenced by
`source/NDanddank_resume.gdoc`. A `.gdoc` is a pointer, not a self-contained document.
Copying its bytes would still refer to the original. Phase 7 uses Drive `files.copy`,
then scoped Google Docs edits, preserving native tables, tab order, headers, footers,
paragraph styles, list structure, fonts, margins, and protected text.

The supplied template was inspected read-only. It has one tab and a 19-row header
table containing the resume. The edit planner found six PROJECTS content paragraphs;
it does not assume that resume text lives in the document body.

## Evidence and configuration

`config/resume.yaml` controls the template pointer, root-confined output directory,
compression limit, optional Sol finalization, and whether a cover letter is required.
Model routes and budgets remain in `config/models.yaml`.

Candidate facts must be current VERIFIED records with valid local source hashes,
quotes, and current project commits. Deterministic retrieval filters facts and projects
against the job requirements before Terra selects fact IDs. Optional Sol finalization
can only reorder or remove the planning selections. It cannot introduce claims.
Projects render exact verified statements; skills render their verified canonical names.
This deliberately requires useful, human-reviewed claim wording in the knowledge base.

The retained template content also requires explicit verification. To prepare its review:

1. Supply a short-lived `GOOGLE_ACCESS_TOKEN` securely in the process environment and
   explicitly set `GOOGLE_DOCS_ALLOW_LIVE=1`. No token is stored in artifacts or task memory.
2. Run `uv run --locked jhm resume template-propose --live-docs --folder-id <private-folder-id>`.
   This reads the source, saves local evidence, and creates an **UNVERIFIED** RESUME_TEMPLATE
   fact. It does not edit the source or grant verification.
3. Inspect the proposal and its source with `jhm catalog facts`. Review the retained
   candidate claims, then use the existing `jhm catalog decide <fact-id> VERIFIED
   --value-sha256 <inspected-hash> --reviewer <reviewer>` command.
4. Set `template_fact_id` in `config/resume.yaml` to that reviewed fact's ID.

Verification is rechecked before native copying and before artifact publication. A revoked
fact, changed protected template content, or changed source evidence prevents FORM_READY.
Do not mark a template VERIFIED merely because it exists or came from a previous resume.

## Execution

All commands run from the fixed project root, using Python 3.12+ and uv:

```bash
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv run --locked jhm resume worker --once
```

This default command only reports offline capability; it does not consume a task.
Tests inject `FakeDocuments` and a scripted mock `ModelGateway`, with sockets blocked.

For explicitly authorized real generation, also supply the existing OpenAI process
credentials and `OPENAI_ALLOW_LIVE=1`, then run:

```bash
uv run --locked jhm resume worker --live-docs --live-models \
  --folder-id <private-folder-id> --once
```

Omit `--once` for the lease/heartbeat worker loop with graceful shutdown and startup recovery.
The application must already have a READY BUILD_RESUME task from qualification.
No form worker or submission implementation is registered by this command.

Native requests are limited to read, copy/create, scoped edit, and PDF export. Document
mutations pass through `resume.actions.ExternalActionService`, using the existing audited
external-actions ledger. No sharing/permissions, email, browser, or submission API is exposed.
The HTTP transport uses fixed Google API origins, bounded timeouts, no redirects, and no
automatic credential refresh. RuntimeMode alone never enables a provider.

## Output and validation

Output directories include Application ID, Task ID, and compression round to prevent
same-company/title/date collisions. Required basenames remain:

```text
resumes/<application-id>/<task-id>/round-N/
  NDanddank_resume_<Company>_<Position>_<MMDDYYYY>.gdoc
  NDanddank_resume_<Company>_<Position>_<MMDDYYYY>.pdf
  NDanddank_resume_<Company>_<Position>_<MMDDYYYY>.snapshot.json

cover-letters/<application-id>/<task-id>/round-N/
  NDanddank_CoverLetter_<Company>_<Position>_<MMDDYYYY>.gdoc
  NDanddank_CoverLetter_<Company>_<Position>_<MMDDYYYY>.pdf
  NDanddank_CoverLetter_<Company>_<Position>_<MMDDYYYY>.snapshot.json
```

Names use the central clock's UTC generation date, frozen for the task across restarts.
Unsafe filename characters become underscores; directory identities prevent collisions.
PathGuard performs every application file write. Existing differing bytes are never silently
overwritten. Source documents, fake stores, snapshots, credentials and outputs remain out of Git.

Each accepted bundle has three artifact rows: PDF, GDOC pointer, native JSON snapshot.
Architecture v2's artifact enum has no GDOC variant, so pointers and snapshots use `OTHER`
with their correct MIME types; no GDOC is mislabeled as DOCX. No schema change is necessary.
Each row has a central ART-prefixed ID and SHA-256. The pointer additionally binds the cloud
document ID, revision, and snapshot hash. A hash of a GDOC pointer alone does not prove content.
Artifacts remain unapproved for submission.

The worker verifies native structure, unchanged protected content, generated text and styles,
then exports PDF and checks that the document did not change during export. Pypdf parses the
actual PDF page tree; text extraction checks that expected content is present, including header
text. A multi-page result drops the lowest-ranked whole fact, rerenders, and counts again.
The loop is bounded and retains at least one project fact and one skill. It never truncates
qualifiers or metrics, shrinks fonts/margins, or reconstructs the resume as DOCX.

If `cover_letter: true`, successful resume publication creates BUILD_COVER_LETTER and holds
the application in the RESUME stage. The cover letter uses the selected resume's verified
facts and static introductory/closing prose. It also has a bounded one-page compression loop.
Only after all configured artifacts validate does one transaction record their hashes,
update application details, append audit events, set FORM_READY, and create READY FORM_PROCESS.
Failure rolls back the entire publication; worker failures alone do not change business state.

## Recovery and limits

Task memory points to immutable, content-addressed run snapshots. IDs, date, policy, selected
facts, model intent/result, copy identity, pre-edit revision, compression round, and artifact
metadata survive restart. Checkpoint graph logic contains none of these effects.

Copy intent is recorded before dispatch. After an uncertain copy, the service searches Drive
appProperties for its action ID; absence or multiple matches requires review instead of a
second blind copy. Edits use a sealed pre-edit revision and readback: an already applied
edit is accepted, a conflicting revision is rejected. A crash after GDOC creation or artifact
publication cannot create duplicate artifact rows or another FORM_PROCESS task.

An interrupted model request is not automatically billed again; existing budget reservations
remain governed by ModelGateway. Unresolved model outcomes, altered artifacts, insufficient
facts, clipping/unextractable PDF text, unsupported template structures, or exhausted
compression require review. WAITING_HUMAN contains an interrupt usable through QueueService's
audited resume API. A resume requests a retry only; it does not approve facts, override
format checks, or release uncertain budgets. Changed policy/selection generally needs a fresh
task after canceling/reconciling the old one, rather than editing its sealed snapshot.

The cloud document can still be edited after export. The snapshot and PDF represent the
validated revision, not a permanent lock on Google Docs. Submission approval and any later
artifact revalidation belong to later phases. Fixed table geometry may make some selections
impossible to fit without a separately reviewed template revision. The integration's live
copy/edit/export rendering has not been exercised against a real application in this phase;
tests use native JSON fixtures and real synthetic PDFs, plus mocked HTTP contracts.

API references: [Google Docs batch updates](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate),
[request ranges and tab/segment targeting](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request),
[Drive file properties](https://developers.google.com/workspace/drive/api/guides/properties),
[Drive export](https://developers.google.com/workspace/drive/api/guides/manage-downloads).
