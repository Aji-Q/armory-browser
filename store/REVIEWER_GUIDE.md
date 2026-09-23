# Reviewer and release QA guide — Armory Browser Companion 0.1.0

Publisher: **Jay Qin** · Support: **jayqin04@gmail.com**.
This is a review guide for a release candidate, **not evidence of Chrome Web Store acceptance**. The private source repository has not been published. The privacy page is [https://aji-q.github.io/armory-privacy/](https://aji-q.github.io/armory-privacy/), publicly deployed and verified on 2026-09-23: the coordinating task opened it in a browser, and this documentation check observed HTTP 200 with the expected title/contact. Recheck content against the final uploaded build.

## Package and prerequisites

The extension ZIP must contain `manifest.json` at its root, all packaged extension modules, the side panel and icons. It must not contain source archives unrelated to the extension, local databases, test secrets, `agent-client.json`, `browser-client.json`, model keys or browser profiles.

The extension has two modes:
1. **Local export:** works without a relay or model-provider account.
2. **Agent task flow:** needs the separately supplied reference relay and MCP adapter, running under Python 3.10+.

**Pre-submission blocker:** the publisher must provide reviewers with a usable authorized backend package and setup instructions, or a controlled reviewer-only HTTPS relay. The extension ZIP alone cannot run the relay. This draft does not invent a public backend download, an already-deployed relay, or working reviewer credentials. Use secure review channels for any temporary credentials; never place them in this guide or the public listing. No paid model account is needed for the HTTP job-creation test below.

## A. Local-only check, without credentials

1. Load the extension using Chrome's Developer mode / Load unpacked, or install the actual review package. Record Chrome version, OS and extension ID.
2. Open a normal public HTML article, click the extension action to open the side panel, then **采集当前页面 · 仅本地导出**.
3. Check the text preview and **导出 Markdown / 导出 JSON**. Confirm source URL and timestamp are meaningful.
4. Confirm no connection to a relay and no page-content upload. Try a restricted `chrome://` page; extraction must fail clearly rather than grant itself more access.
5. The permission prompt / active-tab gesture must work in real Chrome. Mocked extension API tests do not establish this behavior.

## B. Reference relay setup

Obtain the private backend package from the publisher. Set these typed paths to absolute paths on the review machine:

```sh
ARMORY_ROOT='/absolute/path/to/authorized-armory-package'
PYTHON='/absolute/path/to/python3'
PRIVATE_DIR="$HOME/.armory-browser-review"
cd "$ARMORY_ROOT"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10)'
"$PYTHON" -m bridge.relay init --output-dir "$PRIVATE_DIR"
"$PYTHON" -m bridge.relay serve \
  --config "$PRIVATE_DIR/server-config.json" \
  --db "$PRIVATE_DIR/jobs.sqlite" \
  --host 127.0.0.1 --port 8765
```

Initialization refuses to overwrite existing credentials. It prints paths only. Keep this server terminal running. In a private local editor read `browser-client.json`; enter its browser token into the extension's **Browser token** field with Relay URL `http://127.0.0.1:8765`. Click **连接中继** and allow the optional host permission for that relay. Do not send the agent token to the extension or disclose either token in a review screenshot.

Optional CORS tightening after the real extension ID is known: restart the reference relay with `--extension-id` followed by that ID. Do not use a placeholder ID as if it were valid.

## C. Create a test task without an AI account

In a second terminal, set `PYTHON` and `PRIVATE_DIR` to the same values as above. The following creates a task for the public example page using the local agent config; it prints the job ID, not the token. Replace `TEST_URL` only with a public URL controlled by or suitable for the reviewer.

```sh
export PRIVATE_DIR
TEST_URL='https://example.com/'
export TEST_URL
"$PYTHON" - <<'PY'
import json, os, pathlib, urllib.request
config = json.loads((pathlib.Path(os.environ['PRIVATE_DIR']) / 'agent-client.json').read_text())
body = json.dumps({'url': os.environ['TEST_URL'], 'purpose': 'Reviewer: verify authorized browser capture',
                   'human_timeout_seconds': 5}).encode()
request = urllib.request.Request(config['relay_url'] + '/v1/jobs', data=body,
    headers={'Authorization': 'Bearer ' + config['agent_token'], 'Content-Type': 'application/json'}, method='POST')
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
    print(json.load(response)['job']['id'])
PY
```

A short public page can itself be too short for the extraction heuristic; that is not proof of an access bypass. For a positive full-content test, use a reviewer-controlled page with meaningful visible article text. Loopback/private job URLs are intentionally refused by the relay, so do not replace the URL with a local test server and expect a remote-job pass.

## D. Consent, capture and transmission

1. Before site authorization, the task should remain waiting; it must not open/read the requested page merely because the Agent created it.
2. Inspect the site-consent text showing the exact site and destination Relay. Click **授权此站点自动采集与回传 · 8h** and accept the specific optional host permission.
3. Confirm the **自动处理已授权站点** switch is on. Within that consent, tasks should open dedicated background tabs, extract and return automatically. There is **no mandatory per-task Share click**.
4. Check the returned `url`, `title`, `text`, `markdown`, `links`, `captured_at`, `truncated` and quality fields. The final origin must match the task origin. Test a redirect to a different origin: it must not silently capture that destination.
5. In DevTools, inspect the relay request shape without recording tokens. `approve`, `human_required`, `resume` and `preview_ready` must not include page content; only the authorized `complete` event may include the result.
6. Verify no raw HTML, cookies, form values, passwords, page storage or arbitrary remote scripts are sent. Visible text may contain personal information; use synthetic fixtures, not real private accounts.

## E. Human assistance and automatic timeout

Use publisher/reviewer-controlled public HTTPS fixtures: (1) visible article, (2) login-like wall with hidden/insufficient content, (3) an authorized demonstration page that can be made accessible by a harmless local action. No live account password or real CAPTCHA solution is needed to prove the state transitions.

- On a blocked page, confirm `running → awaiting_human` and an explicit countdown. Other authorized tasks must remain able to progress.
- For a human-resume case, create a task with sufficient wait time; open the dedicated tab, complete the harmless fixture action, return to the original origin, and click **已处理，恢复采集** before the deadline.
- For timeout, leave a separate task untouched with `human_timeout_seconds=5`. At/after the relay's deadline, the browser should resume automatically via anonymous public fetching, not ask an Agent to supply credentials or execute page code.
- Public, nonempty fallback content may complete only as `degraded=true` and `quality=partial`. If no usable content exists, expect `failed`, not an empty success or a fabricated complete article.
- A late manual resume must not override the timeout boundary. A login popup over already usable article content should not unnecessarily block capture.
- Close the side panel during the wait. Processing is not promised while closed; reopening/reconnecting with valid consent should handle the expired deadline. Document actual timing instead of calling it always-on execution.

## F. User controls and persistence

- Pause automatic processing, revoke one/all origin grants, and disconnect while a job is pending; no new unauthorized page read or result upload should proceed. A request already sent may have been received.
- Chrome host permission and Armory session consent are separate. Revoking the latter does not remove the former; check the explicit UI explanation.
- Restart Chrome: session tokens, consent and previews should not silently become permanent authority. Local task metadata can persist, but does not itself authorize execution.
- Close a task's dedicated tab: the UI must flag its loss, not capture an unrelated tab. Cancel and recreate the task for a fresh page.
- Completed automatic background pages may be closed only when owned, non-active and not used for human collaboration. Human/active/unrelated tabs must remain untouched.
- Disconnect/uninstall does not recall previously shared results or delete external Agent copies. Reference relay database cleanup is lazy seven-day retention, not a running background eraser.

## G. MCP client checks

Follow `docs/INSTALL_BROWSER.md` for exact local stdio registration commands for Claude Code and Codex. Review each client separately: confirm tool discovery, call `armory_capture`, poll `armory_status`, and cancel a pending job with `armory_cancel`.

The task Relay is **not** a Streamable HTTP MCP endpoint. Do not register `http://127.0.0.1:8765` with a client's HTTP-MCP option. A remote cloud Agent needs a separately secured, reachable HTTPS relay; that deployment is not part of this release candidate.

## H. Permissions / source review

Match the upload to `store/LISTING.md`: `activeTab`, `scripting`, `storage`, `sidePanel`; optional HTTP(S) host patterns. No cookies/history/debugger/webRequest permissions. All executable JavaScript is in the package; server traffic is constrained JSON data. Inspect `extension/manifest.json`, `extension/controller.mjs`, `extension/extract.mjs`, `extension/fallback.mjs`, and the task schema in `docs/browser-contract.json`.

The store requires accurate single-purpose, permission and user-data explanations; optional permissions are not exempt from minimum-permission review. Do not attest to a privacy practice the publisher's actual relay/deployment does not follow. See [Chrome privacy-field guidance](https://developer.chrome.com/docs/webstore/cws-dashboard-privacy) and [Chrome user-data FAQ](https://developer.chrome.com/docs/webstore/program-policies/user-data-faq).

## Current evidence and remaining release checks

The coordinating task reports real Edge DOM fixtures **4/4 passed**. `store/ui-preview-1280x800.png` is a real DOM interface preview; it is not an installed-Chrome screenshot. These findings do not establish Chrome extension installation, browser permission prompts, or the full live agent workflow.

Mark a row complete only after recording the actual build/version, steps, result and evidence location. Unit tests, mocked Chrome APIs, generated promotional art and syntax checks are insufficient.

- [ ] Final ZIP loads in real Chrome without manifest/service-worker errors.
- [ ] Action opens side panel; activeTab local capture/export works.
- [ ] Relay and exact-origin host permission prompts work; denied permission prevents capture.
- [ ] First-origin consent clearly discloses automatic capture **and return**.
- [ ] Automatic article capture and full-result return work end-to-end.
- [ ] Human pause/resume and five-second timeout partial/failed paths work with controlled fixtures.
- [ ] Cancel, revoke, disconnect, cross-origin redirect and tab-close races fail safely.
- [ ] Restart/side-panel-close behavior and storage retention match disclosures.
- [ ] Both actual MCP clients discover and use the three tools; no secrets appear in logs.
- [ ] Actual screenshots match the shipped UI and contain no sensitive data.
- [x] Public privacy URL is deployed and reachable without sign-in (HTTP 200, verified 2026-09-23).
- [ ] Reconfirm the policy accurately describes the exact final uploaded build and any deployed relay.
- [ ] Reviewer backend access is supplied securely; developer account, contact and privacy fields are complete.
- [ ] Submission and Google's review outcome are recorded separately from local validation.
