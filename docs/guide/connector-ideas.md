# Connector starter issues

These are `connector`-labeled starter issues, ready to file. A connector is the first
contribution most people make to Crawfish. Each issue below is self-contained: it gives
you a one-paragraph scope, the base class to subclass, and a typed I/O sketch. To build
one, copy the [Slack worked example](contributing-a-connector.md) and swap in your body.

Every connector must uphold the [security spine](../architecture/SECURITY.md). Targets
are static-only, and credentials are passed by reference: you give the name of an env
var, never the value. Sinks default to `dry_run=True`, so tests run offline.

---

## Slack sink — the worked reference

**Status: done.** It ships as `packages/crawfish-slack/` and is documented in full in
[Contributing a connector](contributing-a-connector.md). Use it as the template for
every connector below.

Scope: post a message to a static Slack channel. It holds the bot token by reference and
records writes in dry-run mode. Base class: `Sink[JSONValue]`. Target:
`channel: str (static)`. Input value: message text. `credential_ref` → bot-token
env var.

---

## Notion sink

Scope: create a page or append a block in a fixed Notion database. This is a clean
target for "summarize each item, then file it in a tracker". You choose the database
once, as a static input; the page contents come from the pipeline output. Hold the
integration token by reference and resolve it only at egress.

- **Base class:** `Sink[JSONValue]`
- **Target (static):** `database_id: str`
- **Input value:** page properties / block content (JSON)
- **Credential:** `credential_ref` → Notion integration token env var

## Gmail source

Scope: fetch the messages matching a static Gmail search query, so a pipeline can
triage or summarize an inbox. The query is fixed at batch start, and results stream
back as fluid items. Emits multiple outputs (`multi=True`).

- **Base class:** `Source[JSONValue]`, `multi=True`
- **Input (static):** `query: str` (e.g. `"label:support is:unread"`)
- **Output:** `messages: list[Email]`
- **Credential:** `credential_ref` → OAuth token reference

## Jira sink

Scope: create or comment on an issue in a fixed Jira project. This is the Atlassian
counterpart to the in-tree Linear sink. The project is static, and the issue fields
come from the output. The base class makes it idempotent, so a re-run won't duplicate
the issue.

- **Base class:** `Sink[JSONValue]`
- **Target (static):** `project_key: str`
- **Input value:** issue fields (summary, description, type)
- **Credential:** `credential_ref` → Jira API token env var

## Postgres source

Scope: stream rows from a static, parameterised query, so a pipeline can fan out over
a table. The SQL text is static and never model-derived; only the bound parameters may
vary. Emits one output per row (`multi=True`).

- **Base class:** `Source[JSONValue]`, `multi=True`
- **Input (static):** `query: str`, optional bound params
- **Output:** `rows: list[Row]`
- **Credential:** `credential_ref` → DSN / connection-string env var

## RSS source

Scope: pull entries from a static feed URL. This is the simplest source there is. It
needs no credential, which makes it a good first contribution. The feed URL is static,
and entries stream as fluid items.

- **Base class:** `Source[JSONValue]`, `multi=True`
- **Input (static):** `feed_url: str`
- **Output:** `entries: list[FeedEntry]`
- **Credential:** none

## Webhook sink

Scope: POST the pipeline output to a static URL. This is a generic egress for any
system that accepts JSON. Keeping the URL static means a prompt can't redirect the
call; the body is the output value. You can optionally sign the payload with a
referenced secret.

- **Base class:** `Sink[JSONValue]`
- **Target (static):** `url: str`
- **Input value:** JSON body
- **Credential:** optional `credential_ref` → signing-secret env var
