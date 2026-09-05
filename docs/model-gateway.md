# Phase 3 ModelGateway

The gateway makes one spending decision before each model request. It chooses an
allowed model, reserves the request's maximum estimated cost, performs the request,
records usage, validates the result, and only then retries or escalates. Agents must
call this interface rather than importing the OpenAI SDK.

## Scope and entry points

- `ModelGateway(database)` defaults to `MockResponsesClient`, even if credentials or
  live opt-in variables are present. An unscripted mock fails closed and never falls
  back to OpenAI.
- `ModelGateway.for_openai(database)` explicitly constructs the live adapter and
  requires **both** `OPENAI_ALLOW_LIVE=1` and `OPENAI_API_KEY` in the process environment.
  The gateway does not search for dotenv files. Constructing it makes no model request.
- `await gateway.structured(...)` is the only model-operation API. It returns the
  requested Pydantic type or a safe exception. `await gateway.close()` closes its client.
- `uv run --offline --locked jhm models` validates and prints registry configuration
  without creating an SDK client or database.

DRY_RUN alone is not live-model consent. Architecture v2 permits model use during a
DRY_RUN, but this phase requires the separate live factory and environment opt-in.
No live-test command is included, no live test was run, and normal tests always block
outbound sockets/DNS. SDK adapter tests use `httpx.MockTransport`.

The existing worker still registers only FAKE and FAKE_HUMAN. There are no model-backed
workflow nodes, Qualification Agent, Slack controls, or browser operations in this phase.

## Registry and routing

`config/models.yaml` is validated with immutable Pydantic settings. It contains the
four architecture tiers, their exact IDs and initial prices, all 14 architecture
routes, confidence thresholds, retry limits, and budgets. Reasoning support is explicit:
Luna/Terra/Sol permit none/low/medium/high/xhigh/max; Astra permits low/medium/high/xhigh/max.
These settings were checked against official model documentation:
[Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra),
[Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol), and
[Astra](https://developers.openai.com/api/docs/models/gpt-6-astra).

Astra is disabled, including when its enabled flag is omitted. Disabled tiers are
never silently skipped to select something more expensive. The default escalation
ceiling is Sol; a caller can lower the ceiling but cannot raise the configured limit.
A deterministic-primary route requires explicit `allow_fallback=True` before using AI.

Schema failure is retried twice on the same tier before escalation. The total default
request limit is six. Transient transport errors retry at the same tier, at most twice;
they do not justify moving to a more expensive model. Backoff uses configured delays
plus up to 20% injected jitter; the SDK itself has retries disabled.

An optional `quality_check` callback can return an explicit `EscalationReason` for
low confidence, an unknown hard rule, contradictory evidence, an unresolved field,
or parser disagreement. Thresholds live in the registry; interpreting job-specific
rules remains the future agent's responsibility. A valid, accepted first result returns
immediately. Refusals are recorded and fail without escalation. Incomplete output is
metered and follows bounded structured-output retries. Exhaustion never returns an
unvalidated object or invented fallback.

## Structured output and prompts

Requests use Responses API `text.format` with a strict JSON schema. The gateway then
independently checks standard JSON, the exact JSON schema, and Pydantic validation.
It rejects extra fields, invalid types, nonfinite numbers, and missing schema-required
fields even if the Pydantic class defines a default. Only local schema references are
accepted, so validation cannot retrieve remote schemas. Use nullable fields to express
unknown values. Open-ended dictionaries are not supported by this strict helper.
The request/response format follows the official
[Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).

`config/prompts.yaml` stores named, semantic-versioned instructions. The shipped generic
prompt is `structured_response` version `1.0.0`; it does not implement qualification.
The prompt registry hashes name/version/instructions. Usage rows store the semantic
version and the SHA-256 of the actual instructions/context/schema combination. The
reservation audit also records prompt name and stable template hash. Change the version
when revising a prompt; immutable source control plus recorded hashes support comparison.

Stable instructions precede dynamic context. Cache keys are SHA-256 digests of the
stable prompt, schema, and caller-supplied namespace; they contain no raw task/candidate
identifiers. Changing context alone preserves the cache key but changes the request hash.
`cache_namespace=None` disables prompt caching through explicit mode with no breakpoints.
Providing a namespace enables implicit caching and sends `prompt_cache_key`. These modes
follow the official [prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching).

The adapter uses the fixed OpenAI endpoint, `store=False`, `service_tier=default`,
`truncation=disabled`, a 30-second timeout, and no tools or streaming. Environment base-URL
and proxy overrides are not used. SDK debug logging is disabled to avoid request-body
disclosure. `store=False` does not assert zero provider retention.

## Price estimation

The architecture's input/cached-input/output rates are preserved. Current GPT-5.6+
cache writes additionally cost 1.25 times ordinary input, so the registry includes
explicit cache-write prices; see official
[pricing](https://developers.openai.com/api/docs/pricing) and
[caching billing](https://developers.openai.com/api/docs/guides/prompt-caching).

Let `I` be total input tokens, `C` cached-read tokens, `W` cache-write tokens, and `O`
output tokens. Let `Pi`, `Pc`, `Pw`, and `Po` be their configured prices per million.
All counts are nonnegative integers and `C + W <= I`. The estimate is:

```text
((I - C - W) × Pi + C × Pc + W × Pw + O × Po) / 1,000,000
```

For Terra, 1,000 input tokens including 200 cached reads and 400 writes, plus 300 output:

```text
ordinary input: 400 × $2.00 / 1,000,000 = $0.00080
cached reads:   200 × $0.20 / 1,000,000 = $0.00004
cache writes:  400 × $2.50 / 1,000,000 = $0.00100
output:        300 × $12.00 / 1,000,000 = $0.00360
total:                                      $0.00544
```

Reasoning tokens are already included in output usage and are not added again.
Arithmetic uses Decimal. The architecture's REAL storage is rounded upward when
necessary to avoid weakening a ceiling. Cache-write counts are recorded in audit
metadata because the authoritative model_usage table has no separate write-token column.
Missing cache-write detail conservatively treats uncached tokens as writes.

## Durable budgets and usage

The initial task limits are QUALIFY_JOB $0.03, BUILD_RESUME $0.30,
BUILD_COVER_LETTER $0.15, FORM_PROCESS $0.20, CONNECT_CONTACTS $0.10, and
MONITOR_APPLICATION $0.02. Application, UTC daily, and UTC monthly limits are $1,
$5, and $75. A task must already exist and its actual task type must have a budget.
Application association comes from the persisted task, never a caller-supplied ID.

Admission uses BEGIN IMMEDIATE and includes existing usage and pending reservations.
It checks all applicable ceilings and inserts a model_usage reservation in the same
transaction. Concurrent processes therefore cannot separately spend the same balance.
The maximum estimate assumes no cache hits, all input written to cache if enabled,
and the full output cap. Input estimation uses UTF-8 byte lengths of instructions,
context, and schema plus 1,024 overhead tokens. Default input/output caps are
16,000/1,024. The configurable input cap cannot exceed 200,000, below the documented
272K long-context pricing boundary.

A pending row has NULL token counts, `success=0`, and the maximum estimated cost.
Settlement updates that same row with reported usage, response ID, estimated cost,
and structured-validation success, together with an append-only audit event. All
paid invalid/refused attempts count toward budgets. `success=1` means valid structured
output, not a business qualification or submission decision.

Definite rejected requests such as rate limits settle at zero cost. Timeouts, ambiguous
server errors, cancellation, process death, or missing usage retain the conservative
reservation. Unknown older-period requests remain charged against current global
limits until reconciled; they are never automatically expired/refunded. Reopening the
database preserves accounting. Mock calls use the same ledger and hypothetical prices;
use an isolated database for examples/tests. Audit metadata distinguishes mock/OpenAI mode.

## Limits and future integration

- This is estimated text-token accounting, not an invoice or provider-side spending
  cap. Prices can change; taxes, negotiated rates, and other provider charges are not
  inferred. No built-in tools, media, long-context, or nonstandard service tier is used.
- The conservative input bound is not a provider token-count guarantee. If reported
  cost exceeds its reservation, actual configured-rate cost is persisted and the gateway
  stops. This cannot undo an already processed request.
- Pending/ambiguous reservations can consume a budget indefinitely. Reconciliation
  is an explicit future administrative operation; raw deletion is not a recovery policy.
- A crash after provider completion but before usage settlement cannot recover exact
  missing usage automatically. The conservative reservation remains. There is no
  cross-provider/database transaction or exactly-once model-call guarantee.
- Call the gateway at an explicit durable I/O boundary. A future LangGraph integration
  must checkpoint model results; do not put an unguarded call in replay-sensitive node logic.
- Prompt and output text are not persisted in model_usage or application logs. Hashes,
  versions, counts, response IDs, and audit context are persisted. SDK exceptions are
  translated to static gateway errors. Registry prices and budgets require trusted editing.
- No model access or live account compatibility was tested. Enabling Astra requires an
  explicit configuration change, an allowed route/ceiling, budget, and live opt-in.
