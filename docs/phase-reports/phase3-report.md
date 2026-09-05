# Phase 3 implementation report

Date: 2026-09-05

Status: **complete — stopped after Phase 3.** No Qualification Agent or Phase 4
service was implemented. No live OpenAI request was made.

## Result

Implemented ModelGateway as the sole repository model-call boundary. It provides
an OpenAI Responses adapter, default scripted mock transport, registry-driven model
selection/reasoning, bounded retries/escalation, structured-output validation,
durable budget reservations, usage persistence, Decimal price estimates, and
versioned/hashed prompts with caching keys.

Read AGENTS.md, Architecture v2's model contracts and surrounding invariants, and
previous reports before implementing. Architecture v2 remains authoritative and
unchanged. Prior phase work is preserved.

## Files created

- `config/models.yaml`: four model definitions, 14 routing operations, reasoning
  support, architecture budgets/confidence thresholds, and retry/output limits.
- `config/prompts.yaml`: generic structured-response prompt, version 1.0.0.
- `src/job_hunting_machine/models/__init__.py`: public gateway interfaces.
- `src/job_hunting_machine/models/gateway.py`: request lifecycle and escalation policy.
- `src/job_hunting_machine/models/client.py`: private Responses adapter and scripted mock.
- `src/job_hunting_machine/models/router.py`: validated configuration and route ceilings.
- `src/job_hunting_machine/models/budgets.py`: atomic reservation/settlement and audit.
- `src/job_hunting_machine/models/pricing.py`: validated token accounting and Decimal prices.
- `src/job_hunting_machine/models/prompts.py`: semantic versions, stable/request hashes,
  and non-sensitive caching keys.
- `src/job_hunting_machine/models/schemas.py`: strict JSON Schema and Pydantic validation.
- `tests/integration/test_model_gateway.py`: 40 acceptance and regression cases.
- `docs/model-gateway.md`: interfaces, accounting semantics, source links, and limitations.
- `docs/phase-reports/phase3-report.md`: this report.

## Files updated

`AGENTS.md`, `.env.example`, `README.md`, `docs/database.md`, `pyproject.toml`, `uv.lock`,
`src/job_hunting_machine/cli.py`, and `tests/unit/test_cli.py` document/expose the current
phase, add dependencies, and provide a read-only `jhm models` inspection command.

## Architecture decisions

1. **Single API boundary.** Only models/client.py imports the OpenAI SDK. Gateway
   policy invokes the private adapter; a source-boundary regression test prevents
   accidental direct SDK imports elsewhere. No workflow/agent is registered for model use.
2. **Exact tiers.** Luna/Terra/Sol/Astra use Architecture v2 IDs/rates. Astra is disabled
   by default and remains disabled if its enabled field is omitted. Caller ceilings
   can only narrow the configured ceiling; disabled tiers never cause a hidden jump.
3. **Structured results.** Responses API uses strict text.format/json_schema. The
   result must pass standard JSON, JSON Schema, and Pydantic validation. Local-only
   schema references prevent validator network retrieval. Refusals are terminal;
   incomplete/schema-invalid output is metered and bounded.
4. **Explicit escalation.** Two schema failures permit the next configured tier.
   Optional quality callbacks must return one of the architecture's explicit reasons.
   Transport failures retry on the same model only. Default global cap is six requests;
   each request needs its own budget reservation. SDK automatic retries are disabled.
5. **Durable accounting without schema redesign.** Existing model_usage rows represent
   both pending maximum-cost reservations and settled usage. Admission uses BEGIN
   IMMEDIATE, checks persisted task/application/global spending, and reserves atomically.
   Settlement and append-only audit commit together. No new table or migration was needed.
6. **Conservative uncertain outcomes.** Process death, cancellation, missing usage,
   and ambiguous transport errors do not release the reservation. Unknown reservations
   remain counted across UTC period boundaries. Definitively rejected calls settle at zero.
7. **Bounded prices.** Estimate configured short-context standard text rates using
   Decimal; cached reads are a subset of total input, and reasoning is already output.
   Current official documentation adds cache-write fees: explicit configuration accounts
   for them while preserving all architecture rates. Cache-write counts live in audit
   metadata, keeping the domain schema unchanged. No cache hit is assumed at admission.
8. **Prompt identity.** Prompts carry names, semantic versions, and stable SHA-256 hashes.
   Usage stores semantic version and rendered request hash; audit includes template/name.
   Caching keys omit dynamic context and raw identifiers. Changing context still changes
   the recorded request hash.
9. **Opt-in live infrastructure.** The default gateway stays mock even when keys and
   opt-in variables exist. Explicit for_openai construction requires OPENAI_ALLOW_LIVE=1
   and OPENAI_API_KEY. No normal test enables networking. HTTP adapter tests mock transport.
10. **Phase separation.** The queue and application business state are not advanced
    by model operations. Qualification routes are configuration only; no qualification
    evaluator, Slack control plane, browser, or external action service was added.

## Documentation verification

The OpenAI Docs skill was used to fetch official Structured Outputs, prompt caching,
pricing, and all four model pages. The implementation also inspected the installed
OpenAI 2.54.0 SDK request/usage definitions. Key source links:

- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
  supports the Responses text.format and schema-validation contract.
- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
  documents keys, explicit/implicit modes, and cache-write accounting.
- [Pricing](https://developers.openai.com/api/docs/pricing) supports the preserved
  architecture rates and added cache-write rates.
- [Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
  [Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra),
  [Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol), and
  [Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)
  support model IDs and reasoning-effort allowlists.

Documentation retrieval and dependency downloads are distinct from application API
calls. No live model access, price billing, or account availability was exercised.

## Commands run

All commands ran from the fixed project root with project-local temporary/cache paths.

```bash
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv add 'openai>=2,<3'
uv add 'jsonschema>=4.23,<5'
uv add --dev 'types-jsonschema>=4,<5'
uv run --offline --locked pytest tests/integration/test_model_gateway.py -q -x
uv run --offline --locked ruff format src tests
uv run --offline --locked pytest -q
uv run --offline --locked ruff check .
uv run --offline --locked ruff format --check .
uv run --offline --locked mypy
uv run --offline --locked jhm models
uv run --offline --locked jhm config
git diff --check
```

Focused tests/formatting/type checks were repeated while correcting implementation
issues. Final dependencies include OpenAI 2.54.0 and jsonschema 4.26.0, locked by uv.
No live test was run or added; any future optional live test must require explicit
OPENAI_ALLOW_LIVE=1 plus a deliberate test selection and credentials.

## Acceptance results

Final full suite: **220 passed**, comprising 180 prior-phase tests and 40 Phase 3 tests.
Ruff passed; Ruff format passed on **64 files**; strict mypy passed on **54 files**.
Git whitespace check passed. All normal tests block outbound network and use synthetic
records in isolated project-local SQLite databases.

| Required scenario | Evidence | Result |
| --- | --- | --- |
| Correct model selected | Qualification route selects Luna/low; HTTP payload verified | PASS |
| Budget blocks excessive call | Budget rejects before transport and before usage insertion | PASS |
| Invalid schema gives bounded retry | Two failures on Luna; no unvalidated result returned | PASS |
| Luna escalates to Terra | Scripted invalid/invalid/valid sequence records all three usages | PASS |
| Escalation ceiling respected | Caller/config limits cap retries at allowed tier | PASS |
| Disabled Astra never selected | Even an Astra route and ceiling cannot enable a disabled tier | PASS |
| Usage row persisted | Model/reasoning, task, application, prompt identity, tokens, cost, response, success, UTC time | PASS |
| Estimated cost correct | Decimal cached/read/write/output arithmetic; no reasoning double count | PASS |
| Mock mode makes no live call | Live constructor blocked; scripted mock cannot fall back to network | PASS |

Additional coverage includes concurrent reservation races, application/daily/monthly
limits, restart/calendar rollover, ambiguous transport costs, rate-limit zero settlement,
refusals, missing usage, cancellation, cost overruns, strict types/defaults/nonfinite
numbers, remote-schema rejection, semantic prompt versions, stable cache keys, routing
configuration validation, and the read-only CLI.

## Known limitations

- Budgets cap conservative local **estimates**, not provider invoices. Input bounds
  use UTF-8 bytes plus overhead, and prices can change. Unexpected overrun is recorded
  and blocks subsequent attempts; it cannot undo a processed request.
- An uncertain request can hold its maximum reservation indefinitely. Automatic
  billing reconciliation is not implemented, and such reservations must not simply expire.
- There is no atomic transaction with OpenAI and no exactly-once/replay cache for
  model results. Future graph integrations must checkpoint gateway results at durable
  I/O boundaries. A crash can leave conservative rather than exact usage accounting.
- Estimates cover configured short-context standard text requests. No images, tools,
  long-context tier, regional endpoint, alternative service tier, or invoice/tax logic.
- Mock calls record hypothetical costs through the same ledger; use isolated databases
  for tests/examples. Operational accounting differs from coding-assistant usage.
- No live model availability or account entitlement was tested. Credentials, output
  content, and candidate documents were not added to Git or usage/audit records.
- Native SQLite confinement/durability limits from earlier reports still apply.

**Phase 3 complete. Stop here; do not implement the Qualification Agent or Phase 4.**
