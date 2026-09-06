# Phase 5: intake, fetch and qualification

The intake worker creates NEW jobs. Qualification workers capture evidence, evaluate
editable rules, and atomically publish a decision. They do not build resumes, submit
applications, or contact employers.

## Running

Initialize policies and schema with `jhm db init`. Existing Slack control produces
RETRIEVE_LINKS tasks. Run `jhm qualify --once` to process one intake task offline.
Without `--once`, the command starts eight workers with one signal/shutdown owner.
The offline CLI leaves QUALIFY_JOB tasks queued, so an empty fake transport cannot
classify real jobs. Tests and Python consumers inject `FakeReader` fixtures.

Public job fetching requires both `--fetch-live` and the process environment variable
`JOB_FETCH_ALLOW_LIVE=1`. Browser fallback additionally requires `--browser-live` and
`JOB_BROWSER_ALLOW_LIVE=1`. Install Chromium explicitly if needed, keeping binaries local:

```bash
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
export PLAYWRIGHT_BROWSERS_PATH="$PWD/data/browser-binaries"
uv run --locked playwright install chromium
```

That installation is optional and was not run during this phase. Keep the same browser
path in the runner's environment. Browser launch uses a temporary profile under the
project root, no stored login, blocked service workers/WebSockets/downloads, no clicks
or forms, and intercepted GET-only requests. Requests are fulfilled through the HTTP
reader so page scripts cannot bypass its address checks. LinkedIn is blocked.

Semantic model use requires `--models-live` and the existing `OPENAI_ALLOW_LIVE=1`
plus securely supplied credentials. Process opt-ins are never automatically read from
`.env`. ModelGateway remains the only OpenAI boundary. Without a supplied gateway,
semantic uncertainty becomes REVIEW.

## Intake and evidence

Canonicalization normalizes host/default ports and unreserved percent escapes, drops
fragments and known tracking parameters, and preserves job-identifying query parameters.
It does not guess equivalence between HTTP/HTTPS, different paths, or ATS URL aliases.
URL uniqueness and qualification task dedupe keys make concurrent/repeated intake safe.
Invalid/private/credential-bearing URLs are excluded. Intake makes zero model calls.

The fetcher tries bounded HTTP GET first, then Schema.org JobPosting JSON/JSON-LD and
visible HTML parsing. An optional Playwright adapter renders incomplete pages. HTTP
redirects are bounded and revalidated; DNS addresses must be public and connections
are pinned to the checked address while preserving Host and TLS SNI. HTTP timeout,
429 and server errors use queue retries. Explicit removal closes the job. Access
challenges create a NEEDS_REVIEW decision and durable WAITING_HUMAN interrupt.
No challenge bypass is attempted.

UTF-8-decoded HTML and extracted text are stored under `evidence/jobs/JOB_ID`, named
by the HTML SHA-256. A task snapshot stores retrieval attempts, URLs, response status,
transport, facts, policy version/rules and evaluation timestamp. Snapshot and evidence
hashes are verified on resume. This preserves source evidence and the exact policy used;
it is not byte-for-byte archival of the original compressed HTTP response.

## Evaluation

Deterministic rules use the enabled rule set and salary policy from SQLite. The loader
requires one enabled rule set. Salary thresholds, country allowlists, internship window,
experience cutoff and start reference come from the seeded editable data. Unknown
operators produce REVIEW. Exactly matching configured occupational terms can pass
without a model. More general occupational, CPT and sponsorship wording may use the
semantic gateway with exact supporting quotes and confidence thresholds.

Examples with the seeded policy:

- A US paid internship must intersect January–August 2027 and have explicit CPT support.
- Standard-city hourly minimum: $20. San Francisco/New York City: $35. Therefore
  $18 < $20 fails, while $40 >= $35 passes the salary rule.
- For a 3–5 year range, the required minimum is 3; 3 < 5 passes the experience rule.
  For 5+ years, 5 >= 5 fails.
- U.S. full-time postings distinguish OPT compatibility, sponsorship support,
  explicit non-support and unknown. Silence never becomes support.

Annual/non-USD/ambiguous pay is reviewed rather than converted using invented hours or
exchange rates. Missing dates, location, experience or open status remain unresolved.
Known deterministic failures skip semantic calls. Semantic interpretation cannot replace
known deterministic facts, and unsupported/missing quotes cannot manufacture evidence.
Routes start with Luna, escalate to Terra, and can reach Sol only when the configured
route, ceiling and budget allow it. Costs and prompts use the existing usage ledger.

## Transactions and recovery

Fetches and model calls occur outside pure graph nodes, with lease heartbeats active.
Persisted snapshots feed the deterministic LangGraph decision node. Checkpoints use the
separate database and the Task ID as thread ID. Final publication rechecks time-sensitive
rules so an application cannot pass an expired deadline after a long interruption.

PASS atomically changes ACTIVE to PASSED, creates Application/Details and a READY
BUILD_RESUME task, and appends audits. FAIL becomes ABORTED with failed rule IDs,
explanations and evidence. Unresolved results become NEEDS_REVIEW. All rule results
and the evidence/policy identity remain auditable. An error creating any application
row rolls back the entire decision transaction. A worker failure alone does not assign
a business rejection. Commit-before-queue-completion replay does not duplicate applications.

An interrupted semantic call is not automatically billed again: a durable started marker
preserves unknowns for review if no result was saved. Already saved results are reused.
Reads interrupted before their snapshot commit may repeat; abandoned content-addressed
files are harmless. Existing bounded retries, stale-lease fencing and shutdown apply.

## Limits

This is a conservative generic parser, not a complete set of ATS adapters. Multiple
JobPosting objects, vague periods such as “summer,” ambiguous qualifications and employer
policy conditions may need human review. There is no outside-company research crawler,
foreign-currency conversion, automatic policy-edit UI, or review-resolution workflow.
Policy edits affect new snapshots; in-flight work retains its policy snapshot. Operators
must manage the enabled policy version explicitly. Salary aliases cover the seeded cities;
unfamiliar metro names require policy curation. Per-domain throttling remains future work.

Fixtures test browser fallback routing; real Chromium/Slack/OpenAI services were not used.
Public GET requests are reads by convention; an untrusted website can implement effects
on GET. No authenticated session, submission endpoint or mutation API is provided here.
Keep evidence and database directories trusted and out of Git. Phase 6 has not started.

Extraction follows [Schema.org JobPosting](https://schema.org/JobPosting). Browser
interception follows Playwright's [network routing documentation](https://playwright.dev/python/docs/network),
including disabling service workers so they cannot escape request interception.
