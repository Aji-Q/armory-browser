# Armory Browser Companion 0.1.1

Private source repository. Only the privacy policy is public. Version 0.1.1 repairs strict TLS, gate detection, result URL identity, unsafe ancillary links, permanent error loops, dynamic readiness and Unicode truncation.

**Automatic capture → human login only when blocked → automatic anonymous fallback on timeout.**

- Chrome Manifest V3 side panel; per-origin session consent, then automatic capture and delivery.
- Single-user local relay with separate browser/agent credentials; standard-library stdio MCP for Claude Code and Codex.
- Original crawler reliability fixes: crawl frontier, content dedup, output collisions, truthful failures, bounded traversal and human handoff.

## Start

See [installation and agent connection](docs/INSTALL_BROWSER.md), [relay reference](bridge/README.md), [architecture and limits](docs/BROWSER_ARCHITECTURE.md), and [store submission materials](store/LISTING.md).

[Public privacy policy](https://aji-q.github.io/armory-privacy/) · Publisher Jay Qin · jayqin04@gmail.com

`dist/armory-browser-0.1.1.zip` contains the extension only. Tokens, inbox snapshots, browser profiles and research run logs are not included.

## Verification boundary

Observed 0.1.1 checks: 208 focused tests, 194 legacy assertions, 13 real Chrome DOM fixtures, a real MCP/HTTP/SQLite simulation, and 5 cross-language real HTTP contract checks. Baseline defects were reproduced again after executable rollback. These layers are verified separately and are not a real installed-extension acceptance test. The screenshot is a clearly labeled interface preview, not evidence of a loaded Chrome extension. Real Chrome installation/permission/login, real Claude Code/Codex interaction and Google review remain acceptance gates. This is a submission candidate, not an approved or production-hosted product.

The side panel must remain open to process tasks. Public relays need HTTPS and encrypted storage; the bundled local SQLite prototype is not a deployed multi-user cloud service. Use only sites and data you are authorized to access.
