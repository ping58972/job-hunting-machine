# Slack control plane — Phase 4

Slack is an input and notification surface. A button records a decision; it cannot
submit an application. The application database remains authoritative.

## Offline use

`SlackControlPlane(database, settings)` uses `FakeSlackAdapter` by default.
`receive(body)` validates and stores an inbox event plus a READY processing task in
one transaction. `process_one()` claims and processes one event. `notify`,
`request_approval`, and `ask_missing` plan messages; `actions.deliver_one()` sends
one planned message through ExternalActionService. Inject Clock for deterministic tests.
`recover()` recovers queue leases and marks uncertain expired sends UNKNOWN_RESULT.

URL input creates a bounded RETRIEVE_LINKS queue payload only. It does not fetch
URLs, create jobs, qualify candidates, or run later phases.

## Explicit live setup

Initialize the database using `jhm db init`. Configure `config/slack.yaml` with the
exact workspace ID, app ID, authorized user IDs, allowed channel IDs, and notification
channel ID. Empty defaults reject all inbound users and channels.

Create a Slack app with Socket Mode and interactivity enabled. Supply an app-level
Socket Mode token and a bot token with permission to post in the selected channel.
Subscribe the app to the message/app-mention events appropriate to that channel and
invite the bot there. Limit granted scopes to the selected event subscriptions.
Consult Slack's [Socket Mode setup](https://docs.slack.dev/tools/bolt-python/concepts/socket-mode)
and [chat.postMessage reference](https://docs.slack.dev/reference/methods/chat.postMessage/).

Supply `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` securely in the process environment;
they are not loaded from `.env`. Set `SLACK_ALLOW_LIVE=1`, then explicitly run:

```bash
uv run --offline --locked jhm slack --live
```

`jhm slack` alone only inspects settings. RuntimeMode alone never enables Slack.
SIGINT/SIGTERM stop the polling loop and close Socket Mode after the active operation.
No live connection was used for Phase 4 validation.

## Persistence and replay

The existing Alembic-owned `slack_events`, `agent_queue`, `approvals`,
`external_actions`, task-memory and activity tables provide persistence; no schema
change or hand-built LangGraph tables are needed.

Bolt persists normalized input before acknowledgment. Storage failures do not receive
successful acknowledgments. Slack's acknowledgment deadline can be exceeded by local
SQLite contention; redelivery is safe through event identity and uniqueness checks.
Only normalized required fields persist, with their SHA-256 hash, never the raw envelope.

Processing runs under the queue's lease fence. Event effects and PROCESSED/REJECTED
status commit together. A crash before queue completion can replay the handler without
repeating its committed effects. Pending input and approvals survive process restart.
Human answers and the matching queue continuation commit in the same transaction.

Outbound requests enter the external-action ledger before sending. A durable ownership
claim prevents simultaneous senders. Successful receipts bind buttons to the exact
channel, message, task, application, approval and payload hash. SDK automatic retries
are disabled. Expired EXECUTING sends become UNKNOWN_RESULT rather than being resent;
a stable provider client message ID is additional protection, not an exactly-once guarantee.

## Approval and confidentiality boundaries

Approval decisions require the configured workspace/app/channel/user allowlists,
a matching delivered message, a pending unexpired approval, valid task/application
correlation, and the unchanged local payload hash. Submission approvals additionally
require READY_TO_REVIEW and no active, uncertain, or successful submission action.
The default maximum approval age is 24 hours. Decisions append audit events and never
change application business state or enqueue submission.

Outbound templates contain fixed status/question text, IDs and review hashes only.
They never include review contents, filesystem paths, human answers, incoming message
text, credentials or exception details. Slack SDK diagnostics are suppressed. Known
credential patterns in URLs and answers are rejected; this is not a general-purpose
secret detector. Users must not paste secrets into Slack. Accepted answers and URLs
remain sensitive local database content and must stay out of Git.

## Limits

Phase 4 does not build review packets, execute submissions, or implement later-phase
agents. Invalidated/expired approvals reject callbacks but remain PENDING; the full
review lifecycle and revocation/consumption services belong to later phases.
UNKNOWN_RESULT/FAILED sends require operator investigation; no automated reconciliation
or resend administration is provided. Queue retries are bounded; exhausted processing
tasks require investigation, and their inbox record may remain PENDING.

SQLite provides a local-process deployment boundary, not a distributed message broker.
Database and review-file directories must remain trusted. File hash checks do not make
an arbitrary local file immutable. Installed SDK behavior is tested offline; workspace
permissions and live Socket Mode delivery still require an explicitly authorized smoke test.
