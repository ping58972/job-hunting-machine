# Application monitor

Phase 11 monitors submitted applications without mutating company portals or Gmail. The scheduler
selects due active applications and creates idempotent `MONITOR_APPLICATION` tasks. Applications in
`REJECTED`, `WITHDRAWN`, `CANCELED`, or `CLOSED` are excluded before task creation and checked again
by the worker before any portal read.

The default schedule follows Architecture v2: one check per day for submitted and under-review
applications, two for interview-stage applications, and zero for terminal applications. A pending
monitor task suppresses another task for the same application. `config/monitor.yaml` also bounds
Gmail messages and portal checks per run. Queue retry delays handle explicitly transient Gmail and
portal errors; exhausted or non-retryable failures use the existing durable queue policy.

Gmail processing searches a bounded recent window for the persisted company and job title. A
message must contain the exact Application ID or deterministic company evidence before it can be
classified. Unrelated mail is ignored. The production semantic classifier calls ModelGateway's
`email_status_classification` route with a Luna ceiling. Portal readers use HTTP GET only, require
public HTTPS destinations, and select Greenhouse, Lever, Ashby, Workday, SmartRecruiters, iCIMS, or
Generic adapters by persisted ATS type and stable host markers. Portal ambiguity uses the separate
Luna-only `portal_status_classification` route.

Every relevant unique observation is saved beneath `evidence/monitor/<application-id>/` through
PathGuard and receives a durable `monitor_events` row. Gmail provider IDs deduplicate messages;
portal URL, status, and content hashes deduplicate unchanged pages. The repository serializes the
dedupe check and insert. Replays therefore do not duplicate an event or state transition.

A transition requires all of these conditions:

- the current application is not terminal;
- the detected status differs from the current status;
- the edge is allowed by the Architecture v2 application state machine;
- confidence is at least 0.90, or 0.95 for rejection, withdrawal, or cancellation.

Observations that are unchanged, low confidence, or invalid transitions remain evidence with
`meaningful_change=0`. They do not update application state and do not produce Slack output. A
valid transition updates both application status columns and appends the observation and transition
audits in the same transaction. Slack notification is planned afterward through the existing
Slack ExternalActionService, once per changed monitor task.

Local scheduling and inert capability inspection:

```bash
uv run --locked jhm monitor schedule
uv run --locked jhm monitor worker
```

Network-free worker fixtures:

```bash
export JHM_RUNTIME_MODE=STAGING
uv run --locked jhm monitor worker --staging --once
```

Live monitoring requires all explicit gates:

```bash
export JHM_RUNTIME_MODE=LIVE
export GMAIL_MONITOR_ALLOW_LIVE=1
export PORTAL_MONITOR_ALLOW_LIVE=1
export OPENAI_ALLOW_LIVE=1
export GMAIL_MONITOR_ACCESS_TOKEN='short-lived-read-only-token'
export OPENAI_API_KEY='key-from-secure-storage'
uv run --locked jhm monitor worker --live --once
```

Use only the Gmail `gmail.readonly` scope for monitoring. Keep tokens in process environment or
secure credential storage; never put them in YAML, evidence, logs, or Git. Slack delivery retains
its independent Phase 4 live transport gate.
