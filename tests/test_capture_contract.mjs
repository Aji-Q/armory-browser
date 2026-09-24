// Cross-layer repair regressions. ARMORY_TEST_ROOT selects an immutable baseline
// or rollback tree while this same test input stays unchanged.
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { setImmediate as yieldLoop } from 'node:timers/promises';
const root = process.env.ARMORY_TEST_ROOT || fileURLToPath(new URL('..', import.meta.url));
const moduleURL = name => pathToFileURL(resolve(root, 'extension', name)).href;
const core = await import(moduleURL('core.mjs'));
const { Controller } = await import(moduleURL('controller.mjs'));
const { Bridge } = await import(moduleURL('bridge.mjs'));
const { anonymousFallback, usablePartialText } = await import(moduleURL('fallback.mjs'));
const RELAY = 'https://relay.example.org', TOKEN = 'synthetic-contract-test-token-only';
const ID = '11111111-1111-4111-8111-111111111111';
const PAGE = 'https://example.com/article/123';
const TEXT = 'The research article presents reproducible observations and discusses its methods and scientific limitations. '.repeat(8);
function job(overrides = {}) { return { id: ID, url: PAGE, purpose: 'Bounded contract fixture', max_chars: 3000,
  state: 'queued', reason: '', result: null, created_at: '2026-09-24T00:00:00Z', updated_at: '2026-09-24T00:00:00Z',
  human_timeout_seconds: 300, human_deadline_at: null, degraded: false, ...overrides }; }
function result(overrides = {}) { return { url: PAGE, title: 'Research article', text: TEXT, markdown: '# Research article\n\n' + TEXT,
  links: [], captured_at: '2026-09-24T00:00:00Z', truncated: false, degraded: false, quality: 'full', ...overrides }; }
function install(t, name, value) {
  const old = Object.getOwnPropertyDescriptor(globalThis, name);
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  t.after(() => old ? Object.defineProperty(globalThis, name, old) : delete globalThis[name]);
}

async function harness(t, options = {}) {
  const jobs = [job(), ...(options.otherJob ? [job({ id: '22222222-2222-4222-8222-222222222222', url: PAGE + '/other' })] : [])];
  const local = {}, session = {}, tabs = new Map(), events = [], injections = [], navigations = [], notices = [];
  const staleID = '33333333-3333-4333-8333-333333333333';
  if (options.staleJob) { local.relayURL = RELAY; local.armoryRecords = { [RELAY]: { [staleID]: job({ id: staleID, url: PAGE + '/expired' }) } }; }
  let expiredReads = 0;
  const originalTimeout = globalThis.setTimeout;
  let now = Date.now(), sleeps = 0, polls = 0;
  t.mock.method(Date, 'now', () => now);
  // Accelerate only short controller waits, preserving scheduling/microtasks and
  // advancing its clock; fetch-abort timeouts remain real and are always cleared.
  install(t, 'setTimeout', (callback, delay, ...args) => {
    if (delay <= 1000) { sleeps++; now += delay; return originalTimeout(callback, 0, ...args); }
    return originalTimeout(callback, delay, ...args);
  });
  const listeners = [];
  function area(state, name) { return {
    async get(keys) { const names = Array.isArray(keys) ? keys : [keys]; return structuredClone(Object.fromEntries(names.filter(key => key in state).map(key => [key, state[key]]))); },
    async set(values) { const changes = {}; for (const [key, value] of Object.entries(values)) { changes[key] = { newValue: structuredClone(value) }; state[key] = structuredClone(value); } for (const listener of listeners) listener(changes, name); },
    async remove(key) { delete state[key]; for (const listener of listeners) listener({ [key]: {} }, name); },
    async setAccessLevel() {},
  }; }
  const chromeMock = {
    storage: { local: area(local, 'local'), session: area(session, 'session'), onChanged: { addListener(listener) { listeners.push(listener); } } },
    permissions: { async request() { return true; }, async contains() { return true; } },
    runtime: { async sendMessage() { return { allowed: true }; } },
    windows: { async update() {} },
    tabs: {
      async create({ url, active }) { const tab = { id: tabs.size + 10, url: options.actualURL && url === PAGE ? options.actualURL : url, active, status: 'complete' }; tabs.set(tab.id, tab); navigations.push(url); return { ...tab }; },
      async get(id) { if (!tabs.has(id)) throw Error('Missing fixture tab'); return { ...tabs.get(id) }; },
      async update(id, change) { Object.assign(tabs.get(id), change); navigations.push(change.url); return { ...tabs.get(id) }; },
      async remove(id) { tabs.delete(id); },
    },
    scripting: { async executeScript({ target }) {
      const tab = tabs.get(target.tabId), own = tab.url === PAGE || tab.url === options.actualURL;
      const index = injections.filter(item => item.id === target.tabId).length;
      injections.push({ id: target.tabId, url: tab.url });
      let output = own && options.outputs ? options.outputs[Math.min(index, options.outputs.length - 1)] : { result: result({ url: tab.url }) };
      if (typeof output === 'function') output = await output();
      if (options.onInjection && own) await options.onInjection({ index, controller, session });
      return [{ result: structuredClone(output) }];
    } },
  };
  install(t, 'chrome', chromeMock);
  install(t, 'fetch', async (address, request = {}) => {
    const url = new URL(address);
    assert.equal(url.origin, RELAY, 'Capture contract harness permits only synthetic relay transport');
    assert.equal(request.headers.Authorization, 'Bearer ' + TOKEN);
    const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
    if (url.pathname === '/v1/browser/jobs') { polls++; return response({ jobs: jobs.filter(item => !['completed', 'failed', 'cancelled'].includes(item.state)) }); }
    const match = url.pathname.match(/jobs\/([0-9a-f-]{36})(\/events)?$/), current = jobs.find(item => item.id === match?.[1]);
    if (!current) { expiredReads++; return response({ error: 'Expired fixture job' }, 404); }
    if (!match[2]) return response({ job: current });
    const body = JSON.parse(request.body);
    events.push({ id: current.id, type: body.type, result: body.result });
    if (body.type === 'complete' && options.rejectComplete) return response({ error: 'Synthetic permanent rejection; body must not leak' }, options.rejectComplete);
    const next = { approve: 'running', human_required: 'awaiting_human', resume: 'running', preview_ready: 'awaiting_share', complete: 'completed', fail: 'failed' };
    current.state = next[body.type]; current.reason = body.reason || ''; current.updated_at = new Date(now).toISOString();
    if (body.type === 'human_required') current.human_deadline_at = new Date(now + 300000).toISOString();
    if (body.type === 'complete') current.result = body.result;
    return response({ job: current });
  });
  const controller = new Controller({ onNotice: (...args) => notices.push(args) });
  await controller.bridge.init(); await controller.connect(RELAY, TOKEN);
  async function settle() {
    for (let index = 0; index < 5000; index++) { if (!controller.busy.size && !controller.polling) return; await yieldLoop(); }
    throw Error('Controller exceeded finite test settling budget');
  }
  async function authorize() { await controller.authorize(ID); await settle(); }
  async function tick() { now += 20000; await controller.tick(); await settle(); }
  return { controller, jobs, tabs, events, injections, navigations, session, notices, authorize, tick, settle,
    get sleeps() { return sleeps; }, get polls() { return polls; }, get expiredReads() { return expiredReads; }, staleID };
}

test('unsafe subsidiary links are dropped while body and safe public links survive relay validation', () => {
  const links = [{ text: 'Public source', url: 'https://example.org/research' },
    ...['http://localhost/a', 'http://127.0.0.1/', 'http://10.1.2.3/', 'https://192.0.2.3/',
      'https://[2001:db8::1]/', 'https://example.com/a?token=sensitive', 'https://example.com/a?X-Amz-Credential=sensitive',
      'https://bad_host.example/', 'https://@example.com/', 'javascript:alert(1)'].map(url => ({ text: 'Unsafe link', url }))];
  const cleaned = core.validateResult(result({ links }), job());
  assert.deepEqual(cleaned.links, [links[0]]);
  assert.equal(cleaned.text, TEXT);
  const probe = spawnSync('python3', ['-c', 'import json,sys; from bridge.relay import validate_result; p=json.load(sys.stdin); validate_result(p["result"],p["job"]); print("RELAY_ACCEPTED")'],
    { cwd: root, input: JSON.stringify({ result: cleaned, job: job() }), encoding: 'utf8' });
  assert.equal(probe.status, 0, probe.stderr);
  assert.equal(probe.stdout.trim(), 'RELAY_ACCEPTED');
});

test('result identity rejects same-origin account/home and changed query', () => {
  assert.throws(() => core.validateResult(result({ url: 'https://example.com/account/home' }), job()));
  assert.throws(() => core.validateResult(result({ url: PAGE + '?id=other' }), job()));
  assert.equal(core.validateResult(result({ url: PAGE + '/#section' }), job()).url, PAGE + '/#section');
});

test('same-origin wrong-page capture does not inject or label dashboard as full article', async t => {
  const h = await harness(t, { actualURL: 'https://example.com/account/home' });
  await h.authorize();
  assert.equal(h.jobs[0].state, 'awaiting_human');
  assert.equal(h.jobs[0].result, null);
  assert.equal(h.injections.length, 0);
});

test('user resume safely navigates owned same-origin tab back to exact task URL', async t => {
  const h = await harness(t, { outputs: [{ humanRequired: true, reason: 'Login required', reasonCode: 'login_wall' }] });
  await h.authorize();
  const tabId = h.controller.bridge.jobs[ID].tabId;
  h.tabs.get(tabId).url = 'https://example.com/account/home';
  const before = h.navigations.length;
  // After login, original article is readable; dashboard itself is never a result.
  globalThis.chrome.scripting.executeScript = async ({ target }) => [{ result: { result: result({ url: h.tabs.get(target.tabId).url }) } }];
  await h.controller.resume(ID); await h.settle();
  assert.equal(h.jobs[0].state, 'completed');
  assert.equal(h.jobs[0].result.url, PAGE);
  assert.equal(h.navigations.length, before + 1);
  assert.equal(h.navigations.at(-1), PAGE);
});

test('permanent complete rejection transitions to failed instead of indefinite awaiting_share retries', async t => {
  const h = await harness(t, { rejectComplete: 400 });
  await h.authorize();
  assert.equal(h.jobs[0].state, 'failed');
  assert.equal(h.jobs[0].result, null);
  await h.tick(); await h.tick();
  assert.equal(h.events.filter(event => event.type === 'complete').length, 1);
  assert.ok(h.events.some(event => event.type === 'fail'));
});

test('authentication rejection creates an explicit local stop rather than retrying forever', async t => {
  const h = await harness(t, { rejectComplete: 403 });
  await h.authorize();
  assert.ok(h.jobs[0].state === 'failed' || h.controller.bridge.jobs[ID].localTerminal === true);
  await h.tick(); await h.tick();
  assert.equal(h.events.filter(event => event.type === 'complete').length, 1);
});

test('bounded dynamic sampling waits through loading and changing body until a stable result', async t => {
  const h = await harness(t, { outputs: [
    { humanRequired: true, reason: 'Article loading', reasonCode: 'insufficient_content' },
    { result: result({ text: TEXT.slice(0, 500) }) }, { result: result() }, { result: result() },
  ] });
  await h.authorize();
  assert.equal(h.jobs[0].state, 'completed');
  assert.equal(h.jobs[0].result.text, TEXT);
  assert.equal(h.events.some(event => event.type === 'human_required'), false);
  assert.ok(h.injections.length >= 4);
  assert.ok(h.injections.length <= 12);
});

test('loading has a hard attempt/time budget and does not prevent another job completing', async t => {
  const h = await harness(t, { otherJob: true, outputs: [{ humanRequired: true, reason: 'Still loading', reasonCode: 'insufficient_content' }] });
  await h.authorize();
  assert.equal(h.jobs[0].state, 'awaiting_human');
  assert.equal(h.jobs[1].state, 'completed');
  const firstTab = h.injections[0]?.id;
  const attempts = h.injections.filter(item => item.id === firstTab).length;
  assert.ok(attempts > 1 && attempts <= 12, `bounded attempts: ${attempts}`);
  const otherComplete = h.events.findIndex(event => event.id === h.jobs[1].id && event.type === 'complete');
  const human = h.events.findIndex(event => event.id === ID && event.type === 'human_required');
  assert.ok(otherComplete >= 0 && otherComplete < human);
});

test('session revocation while awaiting dynamic content stops further read and upload', async t => {
  const h = await harness(t, { outputs: [{ humanRequired: true, reason: 'Loading', reasonCode: 'insufficient_content' }, { result: result() }],
    onInjection: async ({ index, controller }) => { if (index === 0) await controller.revoke('https://example.com'); } });
  await h.authorize();
  assert.equal(h.jobs[0].result, null);
  assert.equal(h.injections.length, 1);
  assert.equal(h.events.some(event => ['preview_ready', 'complete'].includes(event.type)), false);
});

test('anonymous fallback never accepts a different same-origin resource', async t => {
  install(t, 'fetch', async () => { const response = new Response(TEXT, { headers: { 'Content-Type': 'text/plain' } });
    Object.defineProperty(response, 'url', { value: 'https://example.com/account/home' }); return response; });
  await assert.rejects(() => anonymousFallback(job()), /原|任务|页面|resource|正文/);
});


test('browser canonical URL identity permits only existing normalization, not query or resource changes', () => {
  for (const [requested, actual] of [
    ['https://example.com/a/../article/123', PAGE],
    [PAGE + "?q=O'Reilly", PAGE + '?q=O%27Reilly'],
    ['https://[2606:4700:4700:0:0:0:0:1111]/article', 'https://[2606:4700:4700::1111]/article'],
  ]) assert.equal(core.validateResult(result({ url: actual }), job({ url: requested })).url, actual);
  for (const actual of [PAGE + '?x=2&y=1', PAGE + '?x=1&y=3', PAGE + '/other']) {
    assert.throws(() => core.validateResult(result({ url: actual }), job({ url: PAGE + '?x=1&y=2' })));
  }
});

test('expired local job does not poison polling or block a newly authorized fresh job', async t => {
  const h = await harness(t, { staleJob: true });
  await h.authorize();
  assert.equal(h.jobs[0].state, 'completed');
  assert.equal(h.expiredReads, 1, 'A TTL-expired task must not be refetched on every poll');
  assert.equal(h.controller.bridge.jobs[h.staleID]?.localTerminal, true);
  await h.tick();
  assert.equal(h.expiredReads, 1);
});

test('subscriber/member wall text alone is never useful anonymous partial content', () => {
  for (const text of ['This content is available to subscribers. ', 'This article is for paid subscribers only. ',
    'The content is only available to members. ']) assert.equal(usablePartialText(text.repeat(25)), false);
  assert.equal(usablePartialText(TEXT), true);
});

test('explicit challenge is human-required immediately rather than consuming dynamic-loading budget', async t => {
  const h = await harness(t, { outputs: [{ humanRequired: true, reason: 'Human verification', reasonCode: 'access_challenge' }] });
  await h.authorize();
  assert.equal(h.jobs[0].state, 'awaiting_human');
  assert.equal(h.injections.length, 1);
});

test('resume on another origin never navigates or injects into identity-provider content', async t => {
  const h = await harness(t, { outputs: [{ humanRequired: true, reason: 'Login required', reasonCode: 'login_wall' }] });
  await h.authorize();
  h.tabs.get(h.controller.bridge.jobs[ID].tabId).url = 'https://accounts.google.com/login';
  const before = [h.navigations.length, h.injections.length];
  await assert.rejects(() => h.controller.resume(ID));
  assert.deepEqual([h.navigations.length, h.injections.length], before);
  assert.equal(h.jobs[0].state, 'awaiting_human');
});

test('UTF-16 maxChars=100 boundary never creates a lone emoji surrogate', () => {
  assert.equal(typeof core.safeSlice, 'function');
  const text = core.safeSlice('a'.repeat(99) + '🙂' + 'tail', 100);
  assert.equal(text, 'a'.repeat(99));
  const bounded = core.boundedResult(result({ text, markdown: text }), job({ max_chars: 100 }));
  assert.equal(/[\uD800-\uDFFF]/u.test(bounded.text + bounded.markdown), false);
  const probe = spawnSync('python3', ['-c', 'import json,sys; from bridge.relay import validate_result; p=json.load(sys.stdin); validate_result(p["result"],p["job"]); print("UNICODE_ACCEPTED")'],
    { cwd: root, input: JSON.stringify({ result: bounded, job: job({ max_chars: 100 }) }), encoding: 'utf8' });
  assert.equal(probe.status, 0, probe.stderr);
});

test('byte-budget shrinking also preserves complete surrogate pairs', () => {
  const text = 'a'.repeat(99) + '🙂' + 'b'.repeat(24); // 125 code units; 80% cuts inside emoji.
  const payload = result({ title: 'Article', text, markdown: text });
  const input = core.validateResult(payload, job({ max_chars: 125 }));
  const budget = new TextEncoder().encode(JSON.stringify(input)).byteLength - 1;
  const bounded = core.boundedResult(input, job({ max_chars: 125 }), budget);
  assert.equal(bounded.truncated, true);
  assert.equal(/[\uD800-\uDFFF]/u.test(bounded.text + bounded.markdown), false);
  assert.ok(new TextEncoder().encode(JSON.stringify(bounded)).byteLength <= budget);
});

test('anonymous fallback maxChars emoji truncation remains relay-compatible', async t => {
  install(t, 'fetch', async () => { const response = new Response('a'.repeat(99) + '🙂' + 'public tail', { headers: { 'Content-Type': 'text/plain' } });
    Object.defineProperty(response, 'url', { value: PAGE }); return response; });
  const captured = await anonymousFallback(job({ max_chars: 100 }));
  assert.equal(captured.text, 'a'.repeat(99));
  assert.equal(captured.truncated, true);
  assert.equal(/[\uD800-\uDFFF]/u.test(captured.text + captured.markdown), false);
});

test('malformed local result terminates explicitly without recurring submission attempts', async t => {
  const h = await harness(t, { outputs: [{ result: result({ text: '' }) }] });
  await h.authorize();
  assert.equal(h.jobs[0].state, 'failed');
  assert.equal(h.events.some(event => event.type === 'complete'), false);
  await h.tick();
  assert.equal(h.injections.length, 1);
});

test('polling authentication rejection disconnects instead of leaving a background retry loop', async t => {
  const h = await harness(t);
  let calls = 0;
  globalThis.fetch = async () => { calls++; return new Response('{}', { status: 403 }); };
  await h.tick();
  assert.equal(h.controller.bridge.connected, false);
  assert.equal(h.session.browserConnection, undefined);
  await h.tick();
  assert.equal(calls, 1);
});

test('continuously changing usable content never gets mislabeled stable at the time limit', async t => {
  let count = 0;
  const h = await harness(t, { outputs: [() => ({ result: result({ text: TEXT + String(count++) }) })] });
  await h.authorize();
  assert.equal(h.jobs[0].state, 'awaiting_human');
  assert.ok(h.injections.length > 1 && h.injections.length <= 12);
  assert.equal(h.jobs[0].result, null);
});
