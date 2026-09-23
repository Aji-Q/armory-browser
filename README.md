# Armory Browser Companion 0.1.0

Private source repository. Only the privacy policy is public.

**Automatic capture → human login only when blocked → automatic anonymous fallback on timeout.**

- Chrome Manifest V3 side panel; per-origin session consent, then automatic capture and delivery.
- Single-user local relay with separate browser/agent credentials; standard-library stdio MCP for Claude Code and Codex.
- Original crawler reliability fixes: crawl frontier, content dedup, output collisions, truthful failures, bounded traversal and human handoff.

## Start

See [installation and agent connection](docs/INSTALL_BROWSER.md), [relay reference](bridge/README.md), [architecture and limits](docs/BROWSER_ARCHITECTURE.md), and [store submission materials](store/LISTING.md).

[Public privacy policy](https://aji-q.github.io/armory-privacy/) · Publisher Jay Qin · jayqin04@gmail.com

`dist/armory-browser-0.1.0.zip` contains the extension only. Tokens, inbox snapshots, browser profiles and research run logs are not included.

## Verification boundary

Offline crawler regressions, MCP/HTTP/SQLite integration, extension state/controller tests and real Chromium DOM fixture extraction are verified separately. The screenshot is a clearly labeled interface preview, not evidence of a loaded Chrome extension. Real Chrome installation/permission/login, real Claude Code/Codex interaction and Google review remain acceptance gates. This is a submission candidate, not an approved or production-hosted product.

The side panel must remain open to process tasks. Public relays need HTTPS and encrypted storage; the bundled local SQLite prototype is not a deployed multi-user cloud service. Use only sites and data you are authorized to access.
