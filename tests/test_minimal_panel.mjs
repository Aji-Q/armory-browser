#!/usr/bin/env node
// Sidepanel contract/unit tests. Real sidepanel.js in a small fake DOM, with a
// stub Controller; no Chrome APIs, browser automation, network, or UI proof.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = process.env.ARMORY_ROOT || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const html = fs.readFileSync(path.join(ROOT, 'extension/sidepanel.html'), 'utf8');
const source = fs.readFileSync(path.join(ROOT, 'extension/sidepanel.js'), 'utf8')
  .replace(/^import \{ Controller \} from '\.\/controller\.mjs';\r?\n/m, '')
  .replace(/^import \{ hasGrant, humanDeadline, TERMINAL \} from '\.\/core\.mjs';\r?\n/m, '');

class Node {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentElement = null;
    this.attributes = {}; this.dataset = {}; this.listeners = new Map(); this._text = '';
    this.className = ''; this.value = ''; this.disabled = false; this.checked = false;
    this.open = false; this.hidden = false; this.ownerDocument = null;
    this.classList = {
      contains: value => this.className.split(/\s+/).includes(value),
      toggle: (value, force) => {
        const classes = new Set(this.className.split(/\s+/).filter(Boolean));
        const add = force === undefined ? !classes.has(value) : force;
        if (add) classes.add(value); else classes.delete(value);
        this.className = [...classes].join(' '); return add;
      },
      add: value => this.classList.toggle(value, true),
      remove: value => this.classList.toggle(value, false),
    };
  }
  get firstElementChild() { return this.children[0] || null; }
  get childElementCount() { return this.children.length; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'class') this.className = String(value);
    else if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    else if (['hidden', 'disabled', 'open', 'checked'].includes(name)) this[name] = true;
    else this[name] = String(value);
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  append(...nodes) {
    for (const node of nodes) {
      assert.ok(node instanceof Node, 'fake DOM append expects an element');
      node.parentElement = this; node.ownerDocument = this.ownerDocument; this.children.push(node);
    }
  }
  replaceChildren(...nodes) { for (const child of this.children) child.parentElement = null; this.children = []; this.append(...nodes); }
  cloneNode(deep = false) {
    const copy = new Node(this.tagName);
    for (const [key, value] of Object.entries(this.attributes)) copy.setAttribute(key, value);
    copy.className = this.className; copy._text = this._text; copy.ownerDocument = this.ownerDocument;
    if (deep) copy.append(...this.children.map(child => child.cloneNode(true)));
    return copy;
  }
  addEventListener(type, callback) { const callbacks = this.listeners.get(type) || []; callbacks.push(callback); this.listeners.set(type, callbacks); }
  dispatch(type, extra = {}) {
    const event = { target: this, preventDefault() { this.defaultPrevented = true; }, ...extra };
    for (const callback of this.listeners.get(type) || []) callback(event);
    return event;
  }
  matches(selector) {
    if (selector === 'button[data-action]') return this.tagName === 'BUTTON' && this.dataset.action !== undefined;
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    return this.tagName.toLowerCase() === selector;
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  querySelectorAll(selector) { return descendants(this).filter(node => selector.split(',').some(part => node.matches(part.trim()))); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.ownerDocument.activeElement = this; }
  click() {
    if (this.disabled) return;
    if (this.tagName === 'A') this.ownerDocument.downloads.push({ href: this.href, download: this.download });
    this.dispatch('click');
    this.ownerDocument.dispatch('click', { target: this });
  }
}
function descendants(node) { return node.children.flatMap(child => [child, ...descendants(child)]); }
function parseHTML(text) {
  const document = new Node('document'); document.ownerDocument = document;
  document.downloads = []; document.activeElement = null;
  document.createElement = tag => { const node = new Node(tag); node.ownerDocument = document; return node; };
  document.getElementById = id => descendants(document).find(node => node.id === id) || null;
  const stack = [document];
  const voidTags = new Set(['meta', 'link', 'input', 'br', 'hr', 'img']);
  for (const token of text.matchAll(/<!--[\s\S]*?-->|<![^>]*>|<\/[^>]+>|<[^>]+>|[^<]+/g)) {
    const value = token[0];
    if (value.startsWith('<!')) continue;
    if (value.startsWith('</')) {
      const tag = value.slice(2, -1).trim().toUpperCase();
      const index = stack.findLastIndex(node => node.tagName === tag);
      if (index > 0) stack.length = index;
    } else if (value.startsWith('<')) {
      const match = /^<([\w-]+)([\s\S]*?)\/?\s*>$/.exec(value);
      if (!match) continue;
      const node = document.createElement(match[1]);
      for (const attr of match[2].matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) node.setAttribute(attr[1], attr[2] ?? attr[3] ?? attr[4] ?? '');
      stack.at(-1).append(node);
      if (!voidTags.has(match[1].toLowerCase()) && !value.endsWith('/>')) stack.push(node);
    } else stack.at(-1)._text += value;
  }
  return document;
}

const TERMINAL = ['completed', 'failed', 'cancelled'];
const sample = (extra = {}) => ({
  title: 'Readable research note', url: 'https://example.test/article', text: 'A useful body, not a decorative mockup.',
  markdown: '# Readable research note\n\nA useful body, not a decorative mockup.',
  links: [{ text: 'Source', url: 'https://example.test/source' }],
  captured_at: '2026-09-24T12:00:00.000Z', quality: 'full', truncated: false, ...extra,
});
const job = (extra = {}) => ({ id: 'job-12345678', url: 'https://example.test/article', purpose: 'Research', state: 'queued', created_at: '2026-09-24T12:00:00.000Z', ...extra });
function harness() {
  const document = parseHTML(html); const calls = []; const timers = []; const blobs = new Map(); const revoked = [];
  let controller;
  class Controller {
    constructor(options) {
      controller = this; this.options = options; this.bridge = { connected: false, url: '', jobs: {}, previews: {} };
      this.grants = {}; this.autoEnabled = true; this.busy = new Set(); this.localPreview = null; this.interval = null;
      this.captureImpl = () => Promise.resolve();
    }
    init() { calls.push(['init']); this.options.onChange(); return Promise.resolve(); }
    captureLocal() { calls.push(['captureLocal']); return this.captureImpl(); }
    connect(url, token) { calls.push(['connect', url, token]); this.bridge.connected = true; this.bridge.url = url; return Promise.resolve(); }
    disconnect() { calls.push(['disconnect']); this.bridge.connected = false; return Promise.resolve(); }
    toggleAutomatic(value) { calls.push(['toggleAutomatic', value]); this.autoEnabled = value; return Promise.resolve(); }
    authorize(id) { calls.push(['authorize', id]); return Promise.resolve(); }
    resume(id) { calls.push(['resume', id]); return Promise.resolve(); }
    cancel(id) { calls.push(['cancel', id]); return Promise.resolve(); }
    openTab(id) { calls.push(['openTab', id]); return Promise.resolve(); }
    revoke(id) { calls.push(['revoke', id]); return Promise.resolve(); }
    cleanupTabs() { calls.push(['cleanupTabs']); return Promise.resolve(); }
  }
  class TestURL extends URL {
    static createObjectURL(blob) { const url = `blob:test-${blobs.size + 1}`; blobs.set(url, blob); return url; }
    static revokeObjectURL(url) { revoked.push(url); }
  }
  const window = { addEventListener() {} };
  const sandbox = {
    document, window, Controller, TERMINAL, URL: TestURL, Blob, console,
    chrome: { storage: { local: {} }, runtime: { id: 'unit-test-only' } },
    hasGrant: grants => Object.keys(grants).length > 0,
    humanDeadline: value => value.human_deadline || Date.now() + 60_000,
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearInterval() {},
  };
  vm.runInNewContext(source, sandbox, { filename: path.join(ROOT, 'extension/sidepanel.js') });
  const $ = id => { const node = document.getElementById(id); assert.ok(node, `missing #${id}`); return node; };
  return { document, $, controller, calls, blobs, revoked, timers, render: () => controller.options.onChange(), action: (action, id) => {
    const node = document.querySelectorAll('button[data-action]').find(node => node.dataset.action === action && node.dataset.id === id);
    assert.ok(node, `missing ${action} button for ${id}`); node.click(); return node;
  } };
}
async function settle() { for (let i = 0; i < 8; i++) await Promise.resolve(); }
function deferred() { let resolve; let reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
const tests = [];
const test = (name, run) => tests.push({ name, run });

test('Manifest description is plain text within Chrome manifest 132-character limit', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'extension/manifest.json'), 'utf8'));
  assert.equal(typeof manifest.description, 'string');
  assert.ok([...manifest.description].length <= 132, 'Chrome manifest description must not exceed 132 characters');
  assert.ok(!/<[^>]+>/.test(manifest.description));
});
test('Local capture is the first workflow, before optional Relay setup', () => {
  const doc = parseHTML(html); const nodes = descendants(doc);
  const capture = doc.getElementById('local-capture'); const connect = doc.getElementById('connect-form');
  assert.ok(capture && connect, 'both local capture and optional Relay must exist');
  assert.ok(nodes.indexOf(capture) < nodes.indexOf(connect), 'local capture must precede Relay form');
  assert.equal(capture.textContent.trim(), '抓取当前页面');
});
test('Relay details starts collapsed, not as mandatory setup', () => {
  assert.equal(parseHTML(html).querySelector('.connection')?.open, false, 'Relay details must not have open attribute');
});
test('Required operational controls and live feedback remain present', () => {
  const doc = parseHTML(html);
  for (const id of ['connect-form', 'connect-button', 'disconnect-button', 'relay-url', 'browser-token', 'auto-enabled', 'revoke-all', 'cleanup-tabs', 'local-capture', 'jobs-list', 'human-list', 'preview-list', 'grants-list', 'notice']) assert.ok(doc.getElementById(id), `missing #${id}`);
  assert.equal(doc.getElementById('notice').getAttribute('aria-live'), 'polite');
});
test('Empty task and human sections are hidden in initial HTML', () => {
  const doc = parseHTML(html);
  for (const id of ['jobs-section', 'human-section']) assert.equal(doc.getElementById(id)?.hidden, true, `${id} must be initially hidden`);
});
test('Disconnected local capture runs without Relay and indicates pending state', async () => {
  const h = harness(); const pending = deferred(); h.controller.captureImpl = () => pending.promise;
  const button = h.$('local-capture'); button.click();
  assert.deepEqual(h.calls.filter(call => call[0] !== 'init'), [['captureLocal']]);
  assert.equal(button.disabled, true, 'capture button must block duplicate clicks');
  assert.equal(button.textContent, '采集中…'); button.click();
  assert.equal(h.calls.filter(call => call[0] === 'captureLocal').length, 1);
  pending.resolve(); await settle();
  assert.equal(button.disabled, false); assert.equal(button.textContent, '抓取当前页面');
  assert.equal(h.controller.bridge.connected, false);
});
test('Rejected local capture displays feedback and restores the usable button', async () => {
  const h = harness(); h.controller.captureImpl = () => Promise.reject(new Error('用户拒绝授权测试站点'));
  h.$('local-capture').click(); await settle();
  assert.ok(h.$('notice').textContent.includes('用户拒绝授权测试站点'), 'failure notice must preserve the actual error reason');
  assert.equal(h.$('notice').classList.contains('error'), true);
  assert.equal(h.$('local-capture').disabled, false); assert.equal(h.$('local-capture').textContent, '抓取当前页面');
  assert.deepEqual(h.calls.filter(call => call[0] !== 'init'), [['captureLocal']]);
});
test('Failure with an earlier result clearly labels it as old, not a new success', async () => {
  const h = harness(); h.controller.localPreview = sample(); h.render();
  h.controller.captureImpl = () => Promise.reject(new Error('登录后重试'));
  h.$('local-capture').click(); await settle();
  assert.ok(h.$('notice').textContent.includes('本次抓取失败'));
  assert.ok(h.$('notice').textContent.includes('上次结果'));
  assert.ok(h.$('preview-list').textContent.includes('Readable research note'));
});
test('Task sections appear for actual work and hide when only results remain', () => {
  const h = harness();
  assert.equal(h.$('jobs-section').hidden, true); assert.equal(h.$('human-section').hidden, true);
  const queued = job(); h.controller.bridge.jobs = { [queued.id]: queued }; h.render();
  assert.equal(h.$('jobs-section').hidden, false); assert.equal(h.$('human-section').hidden, true);
  queued.state = 'awaiting_human'; h.render();
  assert.equal(h.$('jobs-section').hidden, true); assert.equal(h.$('human-section').hidden, false);
  queued.state = 'completed'; h.controller.bridge.previews[queued.id] = sample(); h.render();
  assert.equal(h.$('jobs-section').hidden, true); assert.equal(h.$('human-section').hidden, true);
  assert.ok(h.$('preview-list').textContent.includes('Readable research note'));
});
test('Periodic render preserves an expanded local preview when content is unchanged', () => {
  const h = harness(); h.controller.localPreview = sample(); h.render();
  const details = h.$('preview-list').querySelector('details'); assert.ok(details); details.open = true;
  h.controller.bridge.jobs = { other: job({ id: 'other' }) }; h.render();
  assert.ok(h.$('preview-list').querySelector('details') === details, 'unrelated task updates must not rebuild preview');
  assert.equal(details.open, true);
});
test('A completed Agent result becomes exportable when its capture lock is released', () => {
  const h = harness(); const completed = job({ state: 'completed' });
  h.controller.bridge.jobs[completed.id] = completed; h.controller.bridge.previews[completed.id] = sample();
  h.controller.busy.add(completed.id); h.render();
  const details = h.$('preview-list').querySelector('details'); details.open = true;
  const buttons = () => h.$('preview-list').querySelectorAll('button[data-action]');
  assert.ok(buttons().find(node => node.dataset.action === 'export-md').disabled);
  h.controller.busy.delete(completed.id); h.render();
  assert.equal(buttons().find(node => node.dataset.action === 'export-md').disabled, false);
  assert.ok(h.$('preview-list').querySelector('details') === details, 'busy changes must preserve the preview node');
  assert.equal(details.open, true);
});
test('Changed preview content is rendered rather than hidden by cache', () => {
  const h = harness(); h.controller.localPreview = sample(); h.render();
  h.controller.localPreview = sample({ text: 'Updated captured body', markdown: 'Updated captured body' }); h.render();
  assert.ok(h.$('preview-list').textContent.includes('Updated captured body'));
  assert.ok(!h.$('preview-list').textContent.includes('A useful body, not a decorative mockup.'));
});
test('Markdown export contains captured content and provenance, then revokes Blob URL', async () => {
  const h = harness(); const result = sample(); h.controller.localPreview = result; h.render(); h.action('export-md', 'local');
  assert.equal(h.document.downloads.length, 1); const download = h.document.downloads[0]; assert.equal(download.download, 'armory-local.md');
  const blob = h.blobs.get(download.href); assert.ok(blob instanceof Blob); assert.equal(blob.type, 'text/markdown;charset=utf-8');
  assert.equal(await blob.text(), `${result.markdown}\n\nSource: ${result.url}\nCaptured: ${result.captured_at}\nQuality: ${result.quality}\n`);
  assert.deepEqual(h.revoked, []); assert.equal(h.timers.length, 1); assert.equal(h.timers[0].delay, 1000);
  h.timers[0].callback(); assert.deepEqual(h.revoked, [download.href]);
});
test('JSON export preserves the full captured result and revokes its Blob URL', async () => {
  const h = harness(); const result = sample({ quality: 'partial', degraded: true });
  const completed = job({ state: 'completed' }); h.controller.bridge.jobs[completed.id] = completed; h.controller.bridge.previews[completed.id] = result; h.render(); h.action('export-json', completed.id);
  const download = h.document.downloads[0]; assert.equal(download.download, `armory-${completed.id}.json`);
  const blob = h.blobs.get(download.href); assert.equal(blob.type, 'application/json;charset=utf-8'); assert.deepEqual(JSON.parse(await blob.text()), result);
  h.timers[0].callback(); assert.deepEqual(h.revoked, [download.href]);
});
test('Expired preview export fails visibly rather than creating an empty download', () => {
  const h = harness(); h.controller.localPreview = sample(); h.render(); h.controller.localPreview = null; h.action('export-md', 'local');
  assert.equal(h.document.downloads.length, 0); assert.equal(h.blobs.size, 0); assert.equal(h.$('notice').textContent, '本地预览不存在或已过期'); assert.equal(h.$('notice').classList.contains('error'), true);
});
test('Authorization, human resume, cancellation and owned-tab routes remain functional', async () => {
  const h = harness(); const value = job(); h.controller.bridge.jobs[value.id] = value; h.render(); h.action('authorize', value.id); h.action('cancel', value.id);
  h.controller.grants = { 'https://example.test': { expiresAt: Date.now() + 100_000 } }; value.state = 'awaiting_human'; value.tabId = 42; h.render(); h.action('resume', value.id); h.action('open', value.id); await settle();
  assert.deepEqual(h.calls.filter(call => call[0] !== 'init'), [['authorize', value.id], ['cancel', value.id], ['resume', value.id], ['openTab', value.id]]);
});
test('Explicit Relay connection clears credentials and collapses setup afterward', async () => {
  const h = harness(); h.$('relay-url').value = 'https://relay.example.test'; h.$('browser-token').value = 'unit-test-token'; h.document.querySelector('.connection').open = true;
  const event = h.$('connect-form').dispatch('submit'); assert.equal(event.defaultPrevented, true); assert.equal(h.$('connect-button').disabled, true); await settle();
  assert.deepEqual(h.calls.filter(call => call[0] === 'connect'), [['connect', 'https://relay.example.test', 'unit-test-token']]);
  assert.equal(h.$('browser-token').value, ''); assert.equal(h.$('connect-button').disabled, false); assert.equal(h.document.querySelector('.connection').open, false);
});
test('Pause, revoke, cleanup and disconnect controls retain their Controller routes', async () => {
  const h = harness(); h.controller.bridge.connected = true; h.controller.grants = { 'https://example.test': { expiresAt: Date.now() + 100_000 } }; h.render();
  h.$('auto-enabled').checked = false; h.$('auto-enabled').dispatch('change'); h.action('revoke', 'https://example.test'); h.$('revoke-all').click(); h.$('cleanup-tabs').click(); h.$('disconnect-button').click(); await settle();
  assert.deepEqual(h.calls.filter(call => call[0] !== 'init'), [['toggleAutomatic', false], ['revoke', 'https://example.test'], ['revoke', undefined], ['cleanupTabs'], ['disconnect']]);
});

console.log('Scope: real sidepanel.js + fake DOM + stub Controller; not a Chrome/browser integration test.');
let passed = 0;
for (const { name, run } of tests) {
  try { await run(); passed++; console.log(`PASS ${name}`); }
  catch (error) { console.log(`FAIL ${name}`); console.error(error.stack || error); }
}
console.log(`Minimal panel: ${passed}/${tests.length} passed`);
process.exitCode = passed === tests.length ? 0 : 1;
