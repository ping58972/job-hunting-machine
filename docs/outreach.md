# Connector and outreach

Phase 10 starts after an application reaches `SUBMITTED`. The submission transaction creates an
independent `CONNECT_CONTACTS` task. Phase 11 monitoring tasks are intentionally outside this
phase.

The Connector Agent accepts bounded public source URLs. It never visits LinkedIn. Its live reader
requires HTTPS, resolves only public addresses, rejects LinkedIn hosts, limits redirects and page
size, and captures the returned HTML under `evidence/contacts/<application-id>/`. Each contact
stores its public source URL, while the audit event records the evidence path and SHA-256.

Extraction is deliberately strict. Structured contact elements and literal `mailto:` addresses
are retained; absent names and titles remain null. Contact type ranking is deterministic:
recruiter, hiring manager, HR, employee, then other. Email and LinkedIn URLs deduplicate within an
application. Conflicting public identities fail instead of merging guesses.

Drafts use the persisted application, job, company, and verified public contact. They make no
candidate accomplishment claims. Email and `LINKEDIN_MANUAL` drafts are stored with canonical
payload hashes. The Connector may create a Gmail draft through the external-action ledger, but it
cannot send it. LinkedIn drafts receive a Slack review record and remain manual; no LinkedIn
browser or send adapter exists.

For email, canonical approval JSON binds:

- recipient email address;
- subject;
- exact body;
- each attachment's Application-scoped artifact ID, filename, MIME type, and SHA-256.

The SEND_EMAIL approval, waiting sender task, and draft binding are created together. Slack only
records an authorized decision and resumes the exact task. The dedicated Outreach Sender checks
LIVE mode, current allowlist membership, expiry, all correlations, current content and attachment
bytes, the Gmail draft ledger row, and the send idempotency key. Changed content revokes approval.

The stable keys are `gmail-draft:<outreach-id>:<payload-hash>` and
`send-email:<outreach-id>:<payload-hash>`. Confirmed send records `SENT`. An ambiguous result is
`UNKNOWN_RESULT` and is not automatically sent again.

Offline capability inspection:

```bash
uv run --locked jhm outreach connector
uv run --locked jhm outreach sender
```

Explicit live operation:

```bash
export JHM_RUNTIME_MODE=LIVE
export CONTACT_DISCOVERY_ALLOW_LIVE=1
export GMAIL_ALLOW_LIVE=1
export GMAIL_ACCESS_TOKEN='short-lived-token-from-secure-storage'
uv run --locked jhm outreach connector --live --once

export OUTREACH_SEND_ALLOW_LIVE=1
uv run --locked jhm outreach sender --live --once
```

Do not store the token in Git or logs. Live Slack delivery retains its separate Socket Mode gates.
