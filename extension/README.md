# Armory Browser Companion 0.1.0

Chrome Manifest V3 / Chrome 116+. This is an unpacked / Chrome Web Store candidate,
not a claim that Google has reviewed or published it.

## Load locally

1. Open chrome://extensions, enable Developer mode, choose “Load unpacked”.
2. Select this extension directory (the directory containing manifest.json).
3. Click its toolbar action to open the side panel.
4. Enter the Relay URL and **browser** token, not the Agent token. HTTPS is required;
   plain HTTP is accepted only for exact localhost, 127.0.0.1, or [::1] development.
5. When the first task for an origin appears, review the request purpose and relay,
   then grant that origin's **automatic capture and return** scope for up to 8 hours.
   The origin authorization covers subsequent tasks on that exact origin.

The extension requires no npm packages, build step, CDN, cookie export, browser
debugging permission, or remote code. The relay and MCP server are separate.

## Real behavior

- While the side panel is open, poll queued tasks every 2 seconds; process up to
  three tasks concurrently. Closing the panel pauses processing. Reopening it
  reconciles remote statuses and expired human deadlines; this is not an
  always-on cloud-controlled browser service.
- Authorized origins: open a dedicated background tab, extract the visible
  article/main content, retain a session preview, and return the result
  automatically. No per-task approval or manual Share click is required.
- Unapproved origins: do not open/read pages or upload content. Show one
  per-origin session consent button and the relay destination.
- Sufficient article content is accepted even if a nonblocking sign-in or
  verification widget also exists. Insufficient content asks for human help.
- Human collaboration remains in the same job/tab. Manually handle the site's
  login, challenge or subscription and return to the original origin; click
  Resume before the server deadline. The extension never fills credentials,
  solves CAPTCHA, or bypasses payment/access restrictions.
- Human timeout defaults to 300 seconds (server-controlled). Waiting does not
  block other jobs. On expiry, return to automatic processing: try a no-cookie
  public HTTP request, maximum 20 seconds / 1.5 MB, with redirects refused.
  Real partial content is tagged degraded:true / quality:partial. Wall text or
  no usable content produces an explicit failed state, not false success.
- Login provider pages and cross-origin redirects are not extracted.
- The local-only capture button never sends that captured result to a relay.
- Completed/cancelled non-human, inactive, tool-owned tabs are cleaned up.
  Active tabs and tabs used for human collaboration are retained. A cleanup
  button handles remaining eligible background tabs.

## Data and permissions

Required permissions: activeTab, scripting, storage, sidePanel.
Optional host permissions are declared for HTTP(S), but are only requested for
the specific relay when connecting and the specific site during a local origin
consent click. No default all-sites grant, cookies, debugger or webRequest.

Chrome host permissions do not distinguish TCP ports. The extension additionally
checks the exact origin (scheme, hostname, port) before extraction and upload.

Tokens, automatic grants and the last 12 captured previews use storage.session.
Relay URL and job metadata/tab linkage use storage.local; no captured body or
token is persisted there. Session grants expire within 8 hours and are
revocable. Disconnect revokes this relay's session scopes; it cannot recall
results already sent. Existing Chrome host permissions remain until removed
in Chrome's extension settings. The automatic-processing switch pauses new
reads/returns but cannot unsend an already issued request.

The connected relay receives task states and authorized text/Markdown/web links.
Agent access and cloud retention are controlled by that relay's owner, not
this extension. Avoid granting private sites to a relay you do not trust.

## Limits and validation

Rendered extraction is a bounded heuristic, not guaranteed semantic extraction.
It omits hidden content, scripts, styles, forms, password/input/textarea values,
editable elements and raw HTML. It does not OCR images/PDFs or traverse
cross-origin frames/shadow DOM. Some restrictive sites deny extension reads.
Anonymous fallback is inert static parsing, cannot execute JavaScript or know
all external CSS visibility, and is therefore always marked partial.

HTTP response size, result sizes, job state transitions, fixed extractor code,
same-origin scope, and session expiry are validated. Large Unicode results are
trimmed to remain within the relay byte limit and marked truncated.

From the parent candidate directory:
    node --test tests/test_extension.mjs

Tests use pure functions / inert mocked DOM, not the user's browser. They do
not prove successful Chrome installation, real login, or Google publication.
