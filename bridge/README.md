# Armory browser relay — single-user prototype

Python 3.10+ standard library only. The relay stores tasks and authorized shared
results; **it never fetches a URL, opens a browser, executes page-provided code,
reads cookies, or performs a login**. The browser extension and its local human
provide those boundary decisions. This is not a production or multi-tenant relay.

## Local setup

Run from the optimized Armory directory:

```sh
python3 -m bridge.relay init --output-dir "$HOME/.armory-browser"
python3 -m bridge.relay serve \
  --config "$HOME/.armory-browser/server-config.json" \
  --db "$HOME/.armory-browser/jobs.sqlite" \
  --host 127.0.0.1 --port 8765
```

`init` refuses to replace existing credentials. It creates three private `0600`
files and prints only their paths, never the tokens:

- `server-config.json`: SHA-256 hashes of two independently generated tokens.
- `agent-client.json`: `{ "relay_url": "...", "agent_token": "..." }`; agent/MCP only.
- `browser-client.json`: `{ "relay_url": "...", "browser_token": "..." }`; local extension only.

Do not give `browser-client.json` to an agent or paste either client file into chat,
source control, logs, or a public site. Do not place credentials in the extension ZIP.
The generated database is `0600`; task/results are not encrypted at rest. SQLite
secure-delete is enabled, but this is not a promise to erase filesystem snapshots
or backups. Token rotation currently means creating a fresh private directory,
updating both clients and restarting with its new server config.

After loading the unpacked extension, optionally restrict CORS to its exact ID:

```sh
python3 -m bridge.relay serve \
  --config "$HOME/.armory-browser/server-config.json" \
  --db "$HOME/.armory-browser/jobs.sqlite" \
  --extension-id aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

The shown ID is a placeholder, not a usable extension identity. Without this flag,
only syntactically valid `chrome-extension://[a-p]{32}` origins are accepted;
all ordinary web origins are rejected. Bearer auth is still required. Requests
without `Origin` are allowed so a local/backend MCP client can connect. CORS
preflight is public but cannot access tasks or results. `/health` exposes only
basic status. All data endpoints require a role-specific bearer token.

## Automatic → human assist → timed automatic fallback

The endpoint and JSON source of truth is `docs/browser-contract.json`. The default
workflow is **automatic capture; ask the human only when blocked; automatically
fall back when the human wait expires**. This is not a per-task approval workflow.
The extension must first obtain local consent scoped to an origin and browser
session for automatic execution and result transfer. Agent credentials cannot
grant that consent, acquire browser host permissions, or send browser events.

1. An agent creates a job using `POST /v1/jobs` with `url`, `purpose`, optional
   `max_chars`, `idempotency_key`, and `human_timeout_seconds` (default 300; integer
   5–900). It receives `201`, or `200` for an identical replay; changed inputs under
   the same key return `409`.
2. The extension lists jobs at `GET /v1/browser/jobs`. Within a locally authorized
   origin/session it may automatically send `approve`, open the matching page,
   and start capture (`queued → running`). Otherwise local authorization remains
   required; the remote agent cannot grant it.
3. A login wall triggers `human_required`, which records a server-clock
   `human_deadline_at` and moves to `awaiting_human`. Credentials stay in the
   browser. A timely human `resume` returns to `running` and clears the deadline.
4. At or after that deadline, only a browser `timeout` event returns the task to
   `running`, with `degraded=true` and a public-content-fallback reason. Early
   timeout and late human resume both return `409`. The relay never fetches or
   circumvents the wall. If the browser closes, the job stays waiting; the next
   browser connection observes the deadline and must submit the fallback event.
5. `preview_ready` changes `running → awaiting_share` **without uploading content**.
   The extension may automatically follow with `complete` under that existing
   origin/session consent, or await an explicit Share outside automatic consent.
   This preserves a local pre-transfer boundary, not a mandatory click per task.
   Only `complete` from `awaiting_share` accepts a whitelisted `RESULT`. The relay
   trusts the browser token and cannot itself prove a local consent gesture.
6. Nonempty public partial content can complete, but both `degraded=true` and
   `quality="partial"` are required after timeout or for any partial result. Full
   nondegraded results may use `quality="full"`. Empty or whitespace-only text
   can never complete; send `fail` when no usable public content exists. The agent
   polls `GET /v1/jobs/{id}` and may cancel via
   `POST /v1/jobs/{id}/cancel`, JSON `{}`. Terminal jobs cannot be resumed or
   overwritten (`409`).

Each `JOB` contains `human_timeout_seconds`, nullable `human_deadline_at` and
boolean `degraded`. Existing first-prototype databases migrate these columns and
preserve task IDs and idempotent replay identities. `RESULT` adds optional
`degraded` and `quality` fields; omitting them preserves legacy full-result shape,
but cannot hide partial or timed-out execution.

All POST requests use `Content-Type: application/json` and a bounded
`Content-Length`. Unknown fields, duplicate JSON keys, credentials fields, raw
HTML in reasons, and upload attempts before sharing are rejected. Reasons are
plain text, at most 1,000 characters. Results have no headers, cookies, storage,
form-value or arbitrary-metadata fields. URLs with embedded userinfo or common
credential-bearing query keys are rejected, not logged. This is defense in depth,
not a guarantee to recognize every possible secret in a page's visible text;
local origin/session consent and visibility into what is shared are essential.

Job URLs must be HTTP(S) on public-looking hosts: literal private/special IPs,
localhost, short/numeric alternate IP spellings and local/internal names are
rejected. No DNS lookup is performed, so hostname policy does not replace browser
permission/origin enforcement or network controls. A result's final origin must
match the original job origin. Returning from a login provider is required before
extraction. Links are HTTP(S), max 200. Body size is at most 600,000 bytes;
`max_chars` is 100–100,000 (default 20,000). There are at most 1,000 active jobs;
queue listing returns the oldest 100. On each store operation, all tasks older
than seven days from creation are deleted, including results and idempotency
records; the next lookup returns `404`. This is lazy TTL cleanup, not a background
retention daemon: stop the service only with awareness that no cleanup runs while
it is stopped.

## Claude Code / Codex and actual cloud agents

The relay itself is provider-independent. Local Claude Code/Codex can use the MCP
adapter from the same project with `agent-client.json`; no provider secrets or
model SDK are needed here. A truly remote/cloud agent **cannot reach your local
127.0.0.1**. Remote use requires an authenticated reachable HTTPS relay or a
carefully configured tunnel plus service hardening; neither is provisioned or
claimed by this prototype. `init --relay-url https://relay.example.com` can prepare
client configuration, but does not deploy anything or configure TLS. Never expose
this development HTTP server directly to the internet. The default loopback bind
is intentional; non-loopback `--host` emits a warning.

## Verification

```sh
python3 -m unittest bridge.test_relay -v
```

Tests use disposable local SQLite storage and a real ephemeral loopback HTTP
server, not external websites. They cover login pause/resume, server-clock timeout fallback, late-resume rejection,
nonempty/partial-result labelling, database migration, pre-transfer privacy,
result submission, role separation, malformed requests, body limits,
strict whitelists, origin checks, concurrent idempotency, cancellation/share race,
queue limits, TTL, persistence, generated credential permissions and non-disclosure.
They do **not** establish Chrome Web Store acceptance, external-cloud connectivity,
or successful extraction from every site.
