import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { relayURL, remoteURL, webURL, permissionPattern, sameOrigin, validateJob, validateResult, boundedResult, nextState, persistentEntry, canCapture, STATES, TERMINAL, makeGrant, hasGrant, humanDeadline, nextAutomaticAction, GRANT_TTL_MS } from '../extension/core.mjs';
import { extractVisiblePage } from '../extension/extract.mjs';
import { usablePartialText } from '../extension/fallback.mjs';

const job = { id: 'bbe1af65-8d12-4c6c-8de5-d81234020a99', url: 'https://example.com/article', purpose: 'Research a public article', max_chars: 1000, state: 'queued', created_at: '2026-09-23T12:00:00Z', updated_at: '2026-09-23T12:00:00Z' };
const result = { url: job.url, title: 'Article', text: 'Visible text', markdown: '# Article\n\nVisible text', links: [{ text: 'Source', url: 'https://example.org/source' }], captured_at: '2026-09-23T12:00:00Z', truncated: false, degraded: false, quality: 'full' };

test('relay HTTPS mandatory, exact loopback HTTP exception', () => {
  assert.equal(relayURL(' https://relay.example.com/ '), 'https://relay.example.com');
  for (const address of ['http://localhost:8731', 'http://127.0.0.1:8731', 'http://[::1]:8731']) assert.equal(relayURL(address), address);
  for (const address of ['http://example.com', 'http://localhost.example.com', 'http://192.168.1.2', 'https://a:b@example.com', 'https://example.com/?token=secret', 'https://example.com/#secret', 'file:///x', 'javascript:alert(1)']) assert.throws(() => relayURL(address));
});

test('remote job URL rejects local, private, mapped IP, and credentials', () => {
  for (const address of ['http://localhost/', 'http://127.0.0.2/', 'http://2130706433/', 'http://0x7f000001/', 'http://10.1.1.1/', 'http://172.16.0.1/', 'http://192.168.5.1/', 'http://169.254.169.254/', 'http://100.64.0.1/', 'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://[fd00::1]/', 'http://[fe80::1]/', 'http://printer.local/', 'http://intranet/', 'https://user:pass@example.com/']) assert.throws(() => remoteURL(address), address);
  assert.equal(remoteURL(job.url).origin, 'https://example.com');
  assert.equal(remoteURL('https://[2606:4700:4700::1111]/').hostname, '[2606:4700:4700::1111]');
});

test('port-independent permission pattern is paired with exact origin check', () => {
  assert.equal(permissionPattern('https://example.com:8443/a'), 'https://example.com/*');
  assert.equal(sameOrigin(job.url, 'https://example.com/other'), true);
  assert.equal(sameOrigin(job.url, 'https://example.com:8443/a'), false);
  assert.equal(sameOrigin(job.url, 'http://example.com/a'), false);
  assert.equal(sameOrigin(job.url, 'https://accounts.google.com/'), false);
});

test('job validation strips remote code and content; ID scope immutable', () => {
  const validated = validateJob({ ...job, script: 'alert(1)', selector: 'body', result, unknown: 'payload' });
  assert.equal(validated.script, undefined);
  assert.equal(validated.result, undefined);
  assert.throws(() => validateJob({ ...job, id: '../../cancel' }));
  assert.throws(() => validateJob({ ...job, state: 'execute' }));
  assert.throws(() => validateJob({ ...job, max_chars: 99 }));
  assert.throws(() => validateJob({ ...job, max_chars: 100001 }));
  assert.throws(() => validateJob({ ...job, url: 'https://other.example/a' }, validated));
  assert.throws(() => validateJob({ ...job, purpose: 'Changed request' }, validated));
  assert.throws(() => validateJob({ ...job, max_chars: 2000 }, validated));
});

test('all allowed state transitions are deterministic and terminal states locked', () => {
  let state = nextState('queued', 'approve');
  assert.equal(state, 'running');
  state = nextState(state, 'human_required');
  assert.equal(state, 'awaiting_human');
  state = nextState(state, 'resume');
  state = nextState(state, 'preview_ready');
  assert.equal(state, 'awaiting_share');
  assert.equal(nextState(state, 'complete'), 'completed');
  assert.throws(() => nextState('queued', 'complete'));
  assert.throws(() => nextState('awaiting_human', 'preview_ready'));
  for (const terminal of TERMINAL) for (const event of ['approve', 'resume', 'complete', 'fail', 'cancel']) assert.throws(() => nextState(terminal, event));
  for (const active of STATES.filter(state => !TERMINAL.includes(state))) {
    assert.equal(nextState(active, 'fail'), 'failed');
    assert.equal(nextState(active, 'cancel'), 'cancelled');
  }
});

test('result validation enforces origin, sizes, web links, timestamp, whitelist', () => {
  assert.deepEqual(validateResult({ ...result, cookie: 'secret', html: '<form>secret</form>' }, job), result);
  assert.throws(() => validateResult({ ...result, url: 'https://id.example.com/login' }, job));
  assert.throws(() => validateResult({ ...result, text: 'x'.repeat(1001) }, job));
  assert.throws(() => validateResult({ ...result, markdown: 'x'.repeat(2001) }, job));
  assert.deepEqual(validateResult({ ...result, links: [{ text: 'X', url: 'javascript:alert(1)' }] }, job).links, []);
  assert.throws(() => validateResult({ ...result, links: Array(201).fill(result.links[0]) }, job));
  assert.throws(() => validateResult({ ...result, captured_at: 'not-a-date' }, job));
});

test('persistent metadata excludes token, previews and arbitrary browser fields', () => {
  const stored = persistentEntry({ ...job, result }, { tabId: 42, approved: true, browser_token: 'secret', preview: result, localError: 'Connection lost' });
  assert.equal(stored.tabId, 42);
  assert.equal(stored.approved, true);
  assert.equal(stored.result, undefined);
  assert.equal(stored.preview, undefined);
  assert.equal(stored.browser_token, undefined);
  assert.equal(JSON.stringify(stored).includes('Visible text'), false);
});

test('capture requires local approval, exact linked tab and original origin', () => {
  const running = { ...job, state: 'running' };
  const local = { tabId: 42, approved: true };
  assert.equal(canCapture(running, { id: 42, url: job.url }, local).allowed, true);
  assert.equal(canCapture(running, { id: 43, url: job.url }, local).allowed, false);
  assert.equal(canCapture(running, { id: 42, url: job.url }, { ...local, approved: false }).allowed, false);
  assert.equal(canCapture(running, { id: 42, url: 'https://accounts.google.com/login' }, local).allowed, false);
  assert.equal(canCapture({ ...job, state: 'cancelled' }, { id: 42, url: job.url }, local).allowed, false);
});

test('web links never accept credentials or non-web execution schemes', () => {
  for (const value of ['data:text/html,hi', 'file:///etc/passwd', 'javascript:1', 'chrome://settings', 'https://a:b@example.com']) assert.throws(() => webURL(value));
});

test('extractor is static and excludes forms, hidden and editable elements', () => {
  const source = readFileSync(new URL('../extension/extract.mjs', import.meta.url), 'utf8');
  for (const forbidden of ['document.cookie', 'localStorage', 'sessionStorage', '.value', 'innerHTML', 'outerHTML', 'eval(', 'new Function']) assert.equal(source.includes(forbidden), false, forbidden);
  for (const required of ['textarea', 'contenteditable', '[hidden]', '[aria-hidden="true"]', 'expectedOrigin', 'humanRequired', 'captured_at', 'truncated']) assert.equal(source.includes(required), true, required);
});

test('session grant is relay-bound, exact-origin, revocable, maximum 8 hours', () => {
  const now = 1000000000;
  const relay = 'https://relay.example.org';
  const grant = makeGrant(relay, job.url, now);
  const grants = { [grant.origin]: grant };
  assert.equal(grant.expiresAt - grant.createdAt, GRANT_TTL_MS);
  assert.equal(hasGrant(grants, relay, job.url, now), true);
  assert.equal(hasGrant({}, relay, job.url, now), false);
  assert.equal(hasGrant(grants, 'https://other-relay.example.org', job.url, now), false);
  assert.equal(hasGrant(grants, relay, 'https://example.com:8443/article', now), false);
  assert.equal(hasGrant(grants, relay, job.url, grant.expiresAt), false);
  assert.equal(hasGrant({ [grant.origin]: { ...grant, expiresAt: grant.expiresAt + 1 } }, relay, job.url, now), false);
  assert.equal(hasGrant({ [grant.origin]: { ...grant, autoShare: false } }, relay, job.url, now), false);
});

test('automatic route only uses authorized scope; pending human never blocks another job', () => {
  const now = Date.parse('2026-09-23T12:00:00Z');
  const relay = 'https://relay.example.org';
  const grant = makeGrant(relay, job.url, now);
  const grants = { [grant.origin]: grant };
  assert.equal(nextAutomaticAction(job, grants, relay, true, now), 'approve_capture');
  assert.equal(nextAutomaticAction(job, {}, relay, true, now), null);
  assert.equal(nextAutomaticAction(job, grants, relay, false, now), null);
  assert.equal(nextAutomaticAction({ ...job, state: 'running' }, grants, relay, true, now), 'capture');
  assert.equal(nextAutomaticAction({ ...job, state: 'awaiting_share' }, grants, relay, true, now), 'share');
  const human = { ...job, state: 'awaiting_human', human_deadline_at: '2026-09-23T12:05:00Z' };
  assert.equal(nextAutomaticAction(human, grants, relay, true, now), null);
  assert.equal(nextAutomaticAction(job, grants, relay, true, now), 'approve_capture');
  assert.equal(nextAutomaticAction(human, grants, relay, true, now + 300000), 'timeout_fallback');
  assert.equal(nextAutomaticAction({ ...job, state: 'running', degraded: true }, grants, relay, true, now), 'anonymous_fallback');
});

test('human deadline uses server timestamp over local value and timeout re-enters automatic route', () => {
  const now = Date.parse('2026-09-23T12:00:00Z');
  assert.equal(humanDeadline({ ...job, human_deadline_at: '2026-09-23T12:00:05Z', humanDeadline: now + 900000 }), now + 5000);
  assert.equal(humanDeadline({ ...job, human_timeout_seconds: 5 }), now + 5000);
  assert.equal(humanDeadline(job), now + 300000);
  assert.equal(nextState('awaiting_human', 'timeout'), 'running');
  assert.throws(() => nextState('running', 'timeout'));
});

function withDOMFixture({ text, password = false, challenge = false, hidden = false }, run) {
  const names = ['document', 'location', 'Node', 'getComputedStyle'];
  const old = Object.fromEntries(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  function el(tag, content = '', properties = {}) {
    return { nodeType: 1, tagName: tag, childNodes: content ? [{ nodeType: 3, textContent: content }] : [], innerText: content, parentElement: null, id: '', className: '', getClientRects: () => [{}], matches: () => false, getAttribute: () => '', hasAttribute: () => false, ...properties };
  }
  const html = el('HTML');
  const body = el('BODY'); body.parentElement = html;
  const article = el('ARTICLE', text); article.parentElement = body;
  if (hidden) article.childNodes.push(el('DIV', 'SECRET HIDDEN CONTENT', { matches: () => true }));
  const input = el('INPUT'); input.parentElement = body;
  const captcha = el('DIV', '', { id: 'captcha', getAttribute: () => '', hasAttribute: () => false }); captcha.parentElement = body;
  Object.defineProperty(globalThis, 'Node', { value: { ELEMENT_NODE: 1, TEXT_NODE: 3 }, configurable: true });
  Object.defineProperty(globalThis, 'location', { value: { href: job.url }, configurable: true });
  Object.defineProperty(globalThis, 'getComputedStyle', { value: () => ({ display: 'block', visibility: 'visible', opacity: '1', contentVisibility: 'visible' }), configurable: true });
  Object.defineProperty(globalThis, 'document', { value: { title: 'Fixture article', body, documentElement: html, querySelectorAll: selector => selector.startsWith('input[') ? (password ? [input] : []) : selector.startsWith('iframe[') ? (challenge ? [captcha] : []) : selector.startsWith('article,') ? [article] : [] }, configurable: true });
  try { return run(); } finally { for (const name of names) if (old[name]) Object.defineProperty(globalThis, name, old[name]); else delete globalThis[name]; }
}

test('sufficient visible article ignores nonblocking password and captcha overlays', () => {
  withDOMFixture({ text: 'Public article with sufficient visible content. '.repeat(20), password: true, challenge: true, hidden: true }, () => {
    const captured = extractVisiblePage('https://example.com', 1000);
    assert.equal(captured.humanRequired, undefined);
    assert.equal(captured.result.quality, 'full');
    assert.equal(captured.result.text.includes('SECRET'), false);
  });
});

test('insufficient login/captcha page requires human without returning wall text', () => {
  for (const kind of ['password', 'challenge']) withDOMFixture({ text: 'Please sign in', [kind]: true }, () => {
    const captured = extractVisiblePage('https://example.com', 1000);
    assert.equal(captured.humanRequired, true);
    assert.equal(captured.result, undefined);
  });
});

test('anonymous partial classifier rejects wall text and empty content', () => {
  assert.equal(usablePartialText('A real public article paragraph about science and research. '.repeat(4)), true);
  assert.equal(usablePartialText('Access denied. '.repeat(40)), false);
  assert.equal(usablePartialText('Sign in to read. '.repeat(40)), false);
  assert.equal(usablePartialText('验证码 '.repeat(40)), false);
  assert.equal(usablePartialText('Short'), false);
});

test('manifest uses least privilege, packaged resources and Chrome 116 MV3', () => {
  const manifest = JSON.parse(readFileSync(new URL('../extension/manifest.json', import.meta.url), 'utf8'));
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.minimum_chrome_version, '116');
  assert.deepEqual(manifest.permissions.sort(), ['activeTab', 'sidePanel', 'scripting', 'storage'].sort());
  assert.deepEqual(manifest.optional_host_permissions, ['http://*/*', 'https://*/*']);
  assert.equal(manifest.host_permissions, undefined);
  assert.equal(manifest.externally_connectable, undefined);
  for (const path of [...Object.values(manifest.icons), manifest.background.service_worker, manifest.side_panel.default_path]) assert.ok(readFileSync(new URL(`../extension/${path}`, import.meta.url)).length > 0);
});

test('public fallback is bounded, credential-free and refuses redirects', () => {
  const source = readFileSync(new URL('../extension/fallback.mjs', import.meta.url), 'utf8');
  for (const required of ["credentials: 'omit'", "redirect: 'error'", '20000', '1500000', "degraded: true, quality: 'partial'"]) assert.ok(source.includes(required));
  for (const forbidden of ['document.cookie', 'localStorage', 'eval(', 'new Function', 'document.body.append']) assert.equal(source.includes(forbidden), false);
});

test('large Unicode result fits server byte limit and honestly marks truncation', () => {
  const big = boundedResult({ ...result, text: '字'.repeat(100000), markdown: '字'.repeat(100000) }, { ...job, max_chars: 100000 });
  assert.ok(new TextEncoder().encode(JSON.stringify(big)).byteLength <= 550000);
  assert.equal(big.truncated, true);
  assert.ok(big.text.length < 100000);
});
