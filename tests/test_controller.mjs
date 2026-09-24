// Controller/Bridge integration with mocked Chrome surfaces and transport only.
// No personal browser, account, remote page, or real relay credential is accessed.
// Run independently: node --test tests/test_controller.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as sleep } from 'node:timers/promises';
import { Controller } from '../extension/controller.mjs';
import { extractVisiblePage } from '../extension/extract.mjs';

const RELAY = 'https://relay.example.org';
const TOKEN = 'synthetic-browser-token-not-a-real-credential';
const ARTICLE = 'A visible public article explains experimental methods, observations, and reproducible scientific findings. '.repeat(12);
const PARTIAL = 'A public abstract describes the scientific topic and its principal observations without access to the full restricted article. '.repeat(3);
const clone = value => structuredClone(value);

function storageArea(area, notify) {
  const state = {};
  return {
    state,
    async get(keys) {
      if (keys == null) return clone(state);
      if (typeof keys === 'string') keys = [keys];
      if (Array.isArray(keys)) return clone(Object.fromEntries(keys.filter(key => key in state).map(key => [key, state[key]])));
      return clone({ ...keys, ...Object.fromEntries(Object.keys(keys).filter(key => key in state).map(key => [key, state[key]])) });
    },
    async set(values) {
      const changes = {};
      for (const [key, value] of Object.entries(values)) {
        const oldValue = clone(state[key]), newValue = clone(value);
        if (JSON.stringify(oldValue) !== JSON.stringify(newValue)) changes[key] = { oldValue, newValue };
        state[key] = newValue;
      }
      if (Object.keys(changes).length) notify(changes, area);
    },
    async remove(keys) {
      const changes = {};
      for (const key of Array.isArray(keys) ? keys : [keys]) {
        if (key in state) changes[key] = { oldValue: clone(state[key]) };
        delete state[key];
      }
      if (Object.keys(changes).length) notify(changes, area);
    },
    async setAccessLevel() {},
  };
}

function withPageFixture(url, fixture, run) {
  const names = ['Node', 'location', 'document', 'getComputedStyle'];
  const originals = Object.fromEntries(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  function element(tag, text = '', extra = {}) {
    return { nodeType: 1, tagName: tag, innerText: text, textContent: text,
      childNodes: text ? [{ nodeType: 3, textContent: text }] : [], parentElement: null,
      matches: () => false, getClientRects: () => [1], getAttribute: () => null,
      hasAttribute: () => false, id: '', className: '', ...extra };
  }
  const html = element('HTML');
  const body = element('BODY'); body.parentElement = html;
  const article = element('ARTICLE', fixture.text ?? ARTICLE); article.parentElement = body;
  body.childNodes = [article];
  const password = element('INPUT'); password.parentElement = body;
  // Reading password values is forbidden even in this controlled fixture.
  Object.defineProperty(password, 'value', { get() { throw new Error('LOGIN_VALUE_MUST_NEVER_BE_READ'); } });
  const challenge = element('DIV', '', { id: 'captcha' }); challenge.parentElement = body;
  const values = {
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 }, location: { href: url },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1', contentVisibility: 'visible' }),
    document: {
      title: fixture.title || 'Controller fixture article', body, documentElement: html,
      querySelectorAll(selector) {
        if (selector.startsWith('input[')) return fixture.password ? [password] : [];
        if (selector.startsWith('iframe[')) return fixture.challenge ? [challenge] : [];
        if (selector.startsWith('article,')) return [article];
        return [];
      },
    },
  };
  for (const name of names) Object.defineProperty(globalThis, name, { value: values[name], configurable: true });
  try { return run(); }
  finally {
    for (const name of names) {
      if (originals[name]) Object.defineProperty(globalThis, name, originals[name]);
      else delete globalThis[name];
    }
  }
}

function harness(t, fixtures = []) {
  const originals = Object.fromEntries(['chrome', 'fetch'].map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const originalNow = Date.now;
  let now = originalNow();
  Date.now = () => now;
  const storageListeners = new Set();
  let sessionEventsDeferred = false;
  const notifyStorage = (changes, area) => {
    if (area === 'session' && sessionEventsDeferred) return;
    queueMicrotask(() => { for (const listener of storageListeners) listener(clone(changes), area); });
  };
  const local = storageArea('local', notifyStorage), session = storageArea('session', notifyStorage);
  const serverJobs = new Map(), pages = new Map(), tabs = new Map(), locks = new Map();
  const events = [], requests = [], navigations = [], captures = [], removedTabs = [], notices = [], permissionRequests = [];
  let tabSequence = 100;
  const terminal = new Set(['completed', 'failed', 'cancelled']);
  for (let index = 0; index < fixtures.length; index++) {
    const fixture = fixtures[index];
    const id = `11111111-1111-4111-8111-${String(index + 1).padStart(12, '0')}`;
    const url = fixture.url || `https://example.com/article-${index + 1}`;
    serverJobs.set(id, { id, url, purpose: 'Synthetic controller integration test', max_chars: 2000,
      state: 'queued', reason: '', result: null, created_at: new Date(now).toISOString(),
      updated_at: new Date(now).toISOString(), human_timeout_seconds: 5, human_deadline_at: null, degraded: false });
    pages.set(url, fixture);
  }
  const chromeMock = {
    storage: { local, session, onChanged: {
      addListener(listener) { storageListeners.add(listener); },
      removeListener(listener) { storageListeners.delete(listener); },
    } },
    permissions: {
      async request(value) { permissionRequests.push(clone(value)); return true; },
      async contains() { return true; },
    },
    runtime: {
      async sendMessage(message) {
        if (message.type === 'armory-lock') {
          if (locks.has(message.key) && locks.get(message.key) !== message.owner) return { allowed: false };
          locks.set(message.key, message.owner); return { allowed: true };
        }
        if (message.type === 'armory-unlock' && locks.get(message.key) === message.owner) locks.delete(message.key);
        return { allowed: true };
      },
    },
    tabs: {
      async create(options) {
        const tab = { id: ++tabSequence, url: options.url, active: options.active, status: 'complete', windowId: 1 };
        tabs.set(tab.id, tab); navigations.push(clone(tab)); return clone(tab);
      },
      async get(id) { if (!tabs.has(id)) throw new Error('Tab not found'); return clone(tabs.get(id)); },
      async remove(id) { removedTabs.push(id); tabs.delete(id); },
      async update(id, changes) { Object.assign(tabs.get(id), changes); return clone(tabs.get(id)); },
      async query() { return [...tabs.values()].filter(tab => tab.active).map(clone); },
    },
    windows: { async update() {} },
    scripting: {
      async executeScript(options) {
        assert.equal(options.func, extractVisiblePage, 'Controller must inject the actual packaged extractor');
        assert.equal(options.world, 'ISOLATED');
        assert.deepEqual(options.target.frameIds, [0]);
        const tab = tabs.get(options.target.tabId);
        assert.ok(tab, 'Only an owned known tab may be captured');
        const fixture = pages.get(tab.url) || {};
        const result = withPageFixture(tab.url, fixture, () => options.func(...options.args));
        captures.push({ tabId: tab.id, url: tab.url, result: clone(result) });
        if (fixture.afterExtraction) await fixture.afterExtraction();
        return [{ result }];
      },
    },
  };
  function jsonResponse(body, status = 200) {
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  }
  async function mockFetch(address, options = {}) {
    const url = new URL(address);
    const body = options.body ? JSON.parse(options.body) : undefined;
    requests.push({ url: url.href, options: { ...options, signal: undefined }, body: clone(body) });
    if (url.origin !== RELAY) {
      assert.equal(options.credentials, 'omit', 'Anonymous fallback must not send site cookies');
      assert.equal(options.redirect, 'error', 'Anonymous fallback must not follow a different origin');
      assert.equal(options.headers.Authorization, undefined, 'Relay token must never be sent to a site');
      const fixture = pages.get(url.href);
      assert.ok(fixture, 'Mock transport rejects every unregistered external destination');
      if (fixture.beforeFallbackResponse) await fixture.beforeFallbackResponse();
      const text = fixture.fallbackText ?? PARTIAL;
      const response = new Response(text, { status: fixture.fallbackStatus || 200, headers: { 'Content-Type': 'text/plain' } });
      Object.defineProperty(response, 'url', { value: url.href });
      return response;
    }
    assert.equal(options.headers.Authorization, `Bearer ${TOKEN}`);
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
    if (url.pathname === '/v1/browser/jobs') return jsonResponse({ jobs: [...serverJobs.values()].filter(job => !terminal.has(job.state)) });
    const match = url.pathname.match(/^\/v1\/(?:browser\/)?jobs\/([0-9a-f-]{36})(\/events)?$/);
    assert.ok(match, 'Controller transport must stay inside its browser/job routes');
    const job = serverJobs.get(match[1]);
    if (!job) return jsonResponse({ error: 'Job not found' }, 404);
    if (!match[2]) return jsonResponse({ job });
    const states = { approve: ['queued', 'running'], human_required: ['running', 'awaiting_human'],
      resume: ['awaiting_human', 'running'], timeout: ['awaiting_human', 'running'],
      preview_ready: ['running', 'awaiting_share'], complete: ['awaiting_share', 'completed'] };
    const event = body.type;
    if (terminal.has(job.state)) return jsonResponse({ error: 'terminal' }, 409);
    if (!['fail', 'cancel'].includes(event) && states[event]?.[0] !== job.state) return jsonResponse({ error: 'transition' }, 409);
    if (event === 'timeout' && now < Date.parse(job.human_deadline_at)) return jsonResponse({ error: 'early timeout' }, 409);
    if (event === 'resume' && now >= Date.parse(job.human_deadline_at)) return jsonResponse({ error: 'late resume' }, 409);
    if (event === 'complete') {
      assert.ok(body.result.text.trim());
      if (job.degraded) assert.deepEqual([body.result.degraded, body.result.quality], [true, 'partial']);
      job.result = clone(body.result);
    } else assert.equal(body.result, undefined, 'Only complete may send page content');
    events.push({ id: job.id, before: job.state, type: event, body: clone(body) });
    job.state = event === 'fail' ? 'failed' : event === 'cancel' ? 'cancelled' : states[event][1];
    job.reason = body.reason || '';
    job.updated_at = new Date(now).toISOString();
    if (event === 'human_required') job.human_deadline_at = new Date(now + job.human_timeout_seconds * 1000).toISOString();
    else job.human_deadline_at = null;
    if (event === 'timeout') job.degraded = true;
    return jsonResponse({ job });
  }
  Object.defineProperty(globalThis, 'chrome', { value: chromeMock, configurable: true });
  Object.defineProperty(globalThis, 'fetch', { value: mockFetch, configurable: true });
  const controller = new Controller({ onNotice: (message, error = false) => notices.push({ message, error }) });
  t.after(() => {
    if (controller.interval) clearInterval(controller.interval);
    assert.equal(controller.busy.size, 0, 'No Controller operation may escape test teardown');
    assert.equal(locks.size, 0, 'Controller must release its task lease');
    Date.now = originalNow;
    for (const name of ['chrome', 'fetch']) {
      if (originals[name]) Object.defineProperty(globalThis, name, originals[name]); else delete globalThis[name];
    }
  });
  async function settle() {
    for (let count = 0; count < 1000; count++) {
      if (!controller.polling && controller.busy.size === 0) { await Promise.resolve(); return; }
      await sleep(5);
    }
    assert.fail('Controller integration exceeded its bounded 5-second settling window');
  }
  async function connect() {
    await controller.init();
    // Use explicit bounded ticks rather than a background interval in this test.
    clearInterval(controller.interval); controller.interval = null;
    await controller.connect(RELAY, TOKEN); await settle();
  }
  async function authorize(id = [...serverJobs.keys()][0]) { await controller.authorize(id); await settle(); }
  async function tick() { await controller.tick(); await settle(); }
  return { controller, serverJobs, pages, tabs, local, session, events, requests, navigations,
    captures, removedTabs, notices, permissionRequests, connect, authorize, tick, settle,
    advance(milliseconds) { now += milliseconds; }, jobs: () => [...serverJobs.values()],
    deferSessionEvents() { sessionEventsDeferred = true; },
    types(id) { return events.filter(event => !id || event.id === id).map(event => event.type); } };
}

test('Controller waits for local scope consent, then auto captures/previews/completes using real Bridge', async t => {
  const h = harness(t, [{ text: ARTICLE, password: true }]);
  await h.connect();
  assert.equal(h.jobs()[0].state, 'queued');
  assert.equal(h.navigations.length, 0);
  assert.equal(h.captures.length, 0);
  await h.authorize();
  const [job] = h.jobs();
  assert.equal(job.state, 'completed');
  assert.deepEqual(h.types(), ['approve', 'preview_ready', 'complete']);
  assert.equal(h.navigations.length, 1);
  assert.equal(h.navigations[0].active, false);
  assert.equal(h.captures.length, 2, 'Two matching usable samples establish bounded readiness');
  assert.equal(job.result.quality, 'full');
  assert.equal(job.result.degraded, false);
  assert.ok(job.result.text.includes('scientific findings'));
  assert.equal(h.removedTabs.length, 1, 'Finished untouched background tab may be cleaned up');
  assert.equal(JSON.stringify(h.local.state).includes(TOKEN), false);
  assert.equal(JSON.stringify(h.local.state).includes('scientific findings'), false);
  assert.ok(h.session.state.armoryPreviews[RELAY][job.id]);
  assert.equal(h.events.filter(event => event.type !== 'complete').some(event => 'result' in event.body), false);
});

test('Real login-wall extraction pauses one job while another authorized job completes', async t => {
  const h = harness(t, [{ text: 'Please sign in', password: true }, { text: ARTICLE }]);
  await h.connect();
  await h.authorize();
  const [blocked, other] = h.jobs();
  assert.equal(blocked.state, 'awaiting_human');
  assert.equal(blocked.result, null);
  assert.ok(blocked.human_deadline_at);
  assert.equal(other.state, 'completed');
  assert.deepEqual(h.types(blocked.id), ['approve', 'human_required']);
  assert.deepEqual(h.types(other.id), ['approve', 'preview_ready', 'complete']);
  assert.equal(h.captures.find(capture => capture.url === blocked.url).result.humanRequired, true);
  assert.ok(h.tabs.has(h.controller.bridge.jobs[blocked.id].tabId), 'Human collaboration tab stays open');
  await h.tick();
  assert.equal(blocked.state, 'awaiting_human', 'Early poll must not trigger fallback');
});

test('Expired human deadline triggers actual anonymous fallback and explicit partial completion', async t => {
  const h = harness(t, [{ text: 'Please sign in', password: true, fallbackText: PARTIAL }]);
  await h.connect(); await h.authorize();
  const [job] = h.jobs();
  assert.equal(job.state, 'awaiting_human');
  h.advance(5001);
  await h.tick();
  assert.equal(job.state, 'completed');
  assert.deepEqual(h.types(), ['approve', 'human_required', 'timeout', 'preview_ready', 'complete']);
  assert.equal(job.degraded, true);
  assert.equal(job.result.degraded, true);
  assert.equal(job.result.quality, 'partial');
  assert.equal(job.result.text, PARTIAL.trim());
  const publicRequests = h.requests.filter(request => !request.url.startsWith(RELAY));
  assert.equal(publicRequests.length, 1);
  assert.equal(publicRequests[0].options.credentials, 'omit');
  assert.equal(publicRequests[0].options.headers.Authorization, undefined);
  assert.equal(h.captures.length, 1, 'Timeout uses anonymous fetch, not another logged-in DOM extraction');
  assert.equal(h.removedTabs.length, 0, 'Human-used tab is not closed after fallback');
});

test('Expired login wall with no public body fails honestly and never submits wall text', async t => {
  const h = harness(t, [{ text: 'Please sign in', password: true, fallbackText: 'Sign in to read. '.repeat(20) }]);
  await h.connect(); await h.authorize();
  h.advance(5001); await h.tick();
  const [job] = h.jobs();
  assert.equal(job.state, 'failed');
  assert.equal(job.result, null);
  assert.deepEqual(h.types(), ['approve', 'human_required', 'timeout', 'fail']);
  assert.equal(h.controller.bridge.previews[job.id], undefined);
  assert.equal(h.events.some(event => event.body.result), false);
});

test('Human resumes before deadline in the same tab and automatic return continues', async t => {
  const h = harness(t, [{ text: 'Please sign in', password: true }]);
  await h.connect(); await h.authorize();
  const [job] = h.jobs();
  const tabId = h.controller.bridge.jobs[job.id].tabId;
  h.pages.set(job.url, { text: ARTICLE });
  h.advance(1000);
  await h.controller.resume(job.id); await h.settle();
  assert.equal(job.state, 'completed');
  assert.deepEqual(h.types(), ['approve', 'human_required', 'resume', 'preview_ready', 'complete']);
  assert.deepEqual(h.captures.map(capture => capture.tabId), [tabId, tabId, tabId]);
  assert.equal(h.navigations.length, 1);
  assert.ok(h.tabs.has(tabId), 'Human collaboration tab remains available');
  assert.equal(job.result.degraded, false);
});

test('Revoking origin during extraction prevents preview and result upload', async t => {
  const fixture = { text: ARTICLE };
  const h = harness(t, [fixture]);
  fixture.afterExtraction = () => h.controller.revoke('https://example.com');
  await h.connect(); await h.authorize();
  const [job] = h.jobs();
  assert.equal(job.state, 'running');
  assert.equal(job.result, null);
  assert.deepEqual(h.types(), ['approve']);
  assert.equal(h.controller.bridge.previews[job.id], undefined);
  await h.tick();
  assert.equal(h.captures.length, 1, 'Revoked scope cannot start another extraction');
  assert.ok(h.notices.some(notice => notice.error && notice.message.includes('授权')));
});

test('Revoking authorization during anonymous fetch prevents fallback content upload', async t => {
  const fixture = { text: 'Please sign in', password: true, fallbackText: PARTIAL };
  const h = harness(t, [fixture]);
  fixture.beforeFallbackResponse = () => h.controller.revoke('https://example.com');
  await h.connect(); await h.authorize();
  h.advance(5001); await h.tick();
  const [job] = h.jobs();
  assert.equal(job.state, 'running');
  assert.equal(job.degraded, true);
  assert.equal(job.result, null);
  assert.deepEqual(h.types(), ['approve', 'human_required', 'timeout']);
  assert.equal(h.controller.bridge.previews[job.id], undefined);
});

test('Remote cancellation during DOM extraction prevents preview and submission', async t => {
  const fixture = { text: ARTICLE };
  const h = harness(t, [fixture]);
  fixture.afterExtraction = async () => { h.jobs()[0].state = 'cancelled'; };
  await h.connect(); await h.authorize();
  const [job] = h.jobs();
  assert.equal(job.state, 'cancelled');
  assert.equal(job.result, null);
  assert.deepEqual(h.types(), ['approve']);
  assert.equal(h.controller.bridge.previews[job.id], undefined);
});

test('Revocation in another sidepanel must prevent this Controller from uploading', async t => {
  const fixture = { text: ARTICLE };
  const h = harness(t, [fixture]);
  await h.connect();
  // A second real Controller shares Chrome session storage, as another window's
  // sidepanel would. Its revocation must invalidate the first panel's in-flight job.
  const otherPanel = new Controller();
  await otherPanel.init();
  clearInterval(otherPanel.interval); otherPanel.interval = null;
  fixture.afterExtraction = async () => {
    await otherPanel.loadGrants();
    await otherPanel.revoke('https://example.com');
  };
  await h.authorize();
  const [job] = h.jobs();
  assert.equal(h.session.state.armoryConsent[RELAY].grants['https://example.com'], undefined);
  assert.ok(job.result === null, 'Session-wide revocation must not leave another panel authorized to send content');
  assert.equal(h.events.some(event => event.type === 'complete'), false);
});


test('Cross-panel revocation is checked against storage even before onChanged delivery', async t => {
  const fixture = { text: ARTICLE };
  const h = harness(t, [fixture]);
  await h.connect();
  const otherPanel = new Controller();
  await otherPanel.init(); clearInterval(otherPanel.interval); otherPanel.interval = null;
  fixture.afterExtraction = async () => {
    await otherPanel.loadGrants();
    h.deferSessionEvents(); // Another execution context's event may arrive later.
    await otherPanel.revoke('https://example.com');
  };
  await h.authorize();
  assert.equal(h.session.state.armoryConsent[RELAY].grants['https://example.com'], undefined);
  assert.ok(h.jobs()[0].result === null, 'Fresh storage authorization must take precedence over stale in-memory grants');
  assert.equal(h.events.some(event => event.type === 'complete'), false);
});

test('Cross-panel auto-pause prevents upload before the storage change event arrives', async t => {
  const fixture = { text: ARTICLE };
  const h = harness(t, [fixture]);
  await h.connect();
  const otherPanel = new Controller();
  await otherPanel.init(); clearInterval(otherPanel.interval); otherPanel.interval = null;
  fixture.afterExtraction = async () => {
    await otherPanel.loadGrants(); h.deferSessionEvents();
    await otherPanel.toggleAutomatic(false);
  };
  await h.authorize();
  assert.equal(h.session.state.armoryConsent[RELAY].autoEnabled, false);
  assert.ok(h.session.state.armoryConsent[RELAY].grants['https://example.com']);
  assert.ok(h.jobs()[0].result === null, 'A session-wide auto-pause must stop a different panel');
  assert.equal(h.events.some(event => event.type === 'complete'), false);
});

test('Cross-panel browser-token removal prevents upload even with an otherwise valid grant', async t => {
  const fixture = { text: ARTICLE };
  const h = harness(t, [fixture]);
  await h.connect();
  const otherPanel = new Controller();
  await otherPanel.init(); clearInterval(otherPanel.interval); otherPanel.interval = null;
  fixture.afterExtraction = async () => {
    h.deferSessionEvents();
    await otherPanel.bridge.disconnect();
  };
  await h.authorize();
  assert.equal(h.session.state.browserConnection, undefined);
  assert.equal(h.session.state.armoryConsent[RELAY].autoEnabled, true);
  assert.ok(h.jobs()[0].result === null, 'A removed session token must invalidate the other panel before upload');
  assert.equal(h.events.some(event => event.type === 'complete'), false);
});

test('Cross-panel revoke during anonymous fallback is enforced without relying on an event', async t => {
  const fixture = { text: 'Please sign in', password: true, fallbackText: PARTIAL };
  const h = harness(t, [fixture]);
  await h.connect();
  const otherPanel = new Controller();
  await otherPanel.init(); clearInterval(otherPanel.interval); otherPanel.interval = null;
  fixture.beforeFallbackResponse = async () => {
    await otherPanel.loadGrants(); h.deferSessionEvents();
    await otherPanel.revoke('https://example.com');
  };
  await h.authorize();
  h.advance(5001); await h.tick();
  assert.equal(h.jobs()[0].degraded, true);
  assert.ok(h.jobs()[0].result === null);
  assert.deepEqual(h.types(), ['approve', 'human_required', 'timeout']);
});
