# Chrome Web Store listing draft — Armory Browser Companion

**Status: release-candidate copy, not a published listing or an approval claim.**
Version: 0.1.0 · Publisher: Jay Qin · Support: jayqin04@gmail.com.
The source repository is private. Do not add a public source-code URL or an open-source claim.

## Store fields

- **Name:** Armory Browser Companion
- **Short description:** Browser research for Claude Code and Codex: automatic capture, human help only when blocked, timed public fallback.
- **Suggested category:** Productivity; confirm the current dashboard choices at submission.
- **Primary interface language:** Simplified Chinese.
- **Minimum Chrome:** 116 (from the submitted manifest).
- **Support email:** jayqin04@gmail.com
- **Privacy policy:** https://aji-q.github.io/armory-privacy/
  Public page deployed and verified on 2026-09-23 (browser check by the coordinating task; documentation check returned HTTP 200 and the expected policy title/contact). Recheck the final package and policy before submission.
- **Homepage/source link:** no public source repository; do not invent a homepage or use a private repository as a public support link.

## Detailed description — English copy

Armory Browser Companion connects your Chrome browser to research tasks from Claude Code or Codex through a separately configured Armory relay and MCP adapter.

Authorize a website once for the current browser session, for up to eight hours. Tasks for that origin then capture and return page content automatically to the relay you selected. New sites wait for your consent. You can pause automation, revoke an origin, or disconnect at any time.

When a login or verification wall actually prevents useful content from being read, Armory asks you to handle it in the dedicated browser tab. Your website credentials stay in the browser. If the human wait expires, the extension attempts an anonymous public-content fallback. Any available partial content is clearly labelled; inaccessible pages fail explicitly instead of being reported as complete. Other tasks can continue.

You can also capture the current page for local Markdown or JSON export without sending it to a relay.

Important requirements and limits:
- Chrome 116 or later. The interface is currently in Simplified Chinese.
- Agent-connected use requires a separately configured single-user relay and Python 3.10+ MCP adapter. This is not a hosted service, and installation alone does not connect a cloud agent.
- Automatic processing runs while the side panel is open and connected. It is not an always-on background crawler.
- Website support varies. Armory does not bypass logins, paywalls, CAPTCHAs or access restrictions, and does not promise complete extraction from every site.

Data disclosure: authorized tasks send the page URL, title, extracted text/Markdown, selected links, capture time and result-quality metadata to your configured relay so your agent can use them. This may include personal information present in visible page content. Within an authorized origin/session, return is automatic—not an approval click for each page. Local-only exports are not uploaded. The extension does not request cookie, browser-history, debugger or web-request permissions, and does not upload website passwords, form values, cookies, browser storage or raw HTML. Relay authentication tokens are kept in browser-session storage. The current build has no advertising or analytics SDK.

Armory Browser Companion is an independent project by Jay Qin, not an official product of OpenAI, Anthropic or Google. Support: jayqin04@gmail.com.

## Single-purpose field

Capture content from user-authorized web pages for a user's research workflow, returning structured results to that user's configured agent relay or exporting them locally, with limited human assistance when access is blocked.

## Permissions justifications

These entries must stay synchronized with the exact uploaded `extension/manifest.json`.

| Manifest permission | Justification to adapt into the dashboard |
|---|---|
| `activeTab` | Temporary access to the current page after the user invokes the extension, for explicit local-only extraction/export. It is not a background grant over unrelated tabs. |
| `scripting` | Execute the packaged, fixed extraction function in the main frame of the selected/authorized page. The relay cannot supply executable code. |
| `storage` | Keep relay settings and limited task metadata locally; keep the browser token, expiring origin consent and extracted previews in session storage. No Chrome sync storage is used. |
| `sidePanel` | Display the task queue, site-consent controls, human-assistance actions and captured results beside the page. |
| Optional `http://*/*`, `https://*/*` | Users choose their own relay and research websites. The extension requests host access at the local connect/authorize gesture for a specific origin's host pattern, not blanket access to all websites at installation. Application consent additionally checks the exact scheme/host/port and expires after at most eight hours. HTTP is needed for local loopback relay development and user-authorized HTTP websites; remote relay connections require HTTPS. |

No `cookies`, `history`, `tabs`, `debugger`, `webRequest`, `downloads` or `nativeMessaging` permission is declared. The extension can create/manage its own task tabs using the tab operations available with its declared permissions; this is not a claim that it never uses the Tabs API.

## Data-use disclosure worksheet

Do **not** select “no user data collected.” Collection here includes processing on-device and transferring to a user-selected service. The final dashboard answers must match the deployed privacy policy and actual distribution configuration, not merely these suggested categories. [Chrome privacy-field guidance](https://developer.chrome.com/docs/webstore/cws-dashboard-privacy).

| Data | What this build does |
|---|---|
| Website content | Reads authorized rendered page content; transfers whitelisted text/Markdown, title and links for Agent tasks; keeps previews in session storage. Anonymous timeout fallback processes public response content separately and marks it partial. |
| Task URLs / web activity | Stores and sends requested/captured URLs and task metadata needed for the visible feature. It does not enumerate Chrome browsing history or monitor every visited page. Disclose relevant “Web history” collection rather than claiming no URLs are processed. |
| Authentication information | Accepts a user-supplied Armory browser token for relay authentication; keeps it in `storage.session` and sends it only as authentication to the chosen relay. Website passwords, cookies and login form values are not extracted. |
| Personal or sensitive content | Page text/title/URL may contain names, email addresses, personal communications, financial, health or location information. There is no universal sensitive-text redactor. Review all applicable dashboard categories against allowed use cases; do not exclude a category merely because it appears inside page text rather than in a dedicated form. |
| Recipients | The user's configured relay; the Agent/AI provider the user connects to that relay. In local-export-only mode, there is no relay upload. There is no built-in developer-hosted collection endpoint. |
| Retention | Browser local task metadata persists until removed/bounded history replacement; tokens/consent/previews are session-scoped. Reference relay storage is unencrypted SQLite with private file permissions and lazy seven-day cleanup. Agent copies, user exports and backups have independent retention. |
| User controls | Pause automation, revoke site consent, disconnect, cancel pending tasks, use local export only, or uninstall. These actions do not recall results already delivered to a relay or Agent. |

The current build does not sell data or include advertising/tracking code. Only attest to Google's limited-use statements after confirming publisher operations and any separately deployed relay follow those commitments. Minimum-permission and disclosure obligations apply even when permissions are optional. [Chrome user-data FAQ](https://developer.chrome.com/docs/webstore/program-policies/user-data-faq).

## Assets and release gates

- Extension icons are included at 16, 32, 48 and 128 pixels.
- `store/promo-440x280.png` is promotional artwork, **not proof of actual Chrome operation**.
- `store/ui-preview-1280x800.png` is an actual DOM interface preview, **not a screenshot proving an installed Chrome extension**. Do not mislabel it as installation or permission validation.
- Capture final screenshots from the installed release candidate in real Chrome, using fixtures or redacted pages; never show tokens, private page contents or real login credentials.
- Verify the public privacy URL, official developer account fields, actual permissions, screenshot accuracy and reviewer setup before submission.
- No “approved,” “official integration,” “all websites,” “unlimited,” “24/7 background,” or guaranteed-performance claim belongs in this listing.
