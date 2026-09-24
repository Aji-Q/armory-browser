// Real Controller + packaged extractor; only Chrome/DOM are in-memory fixtures.
// No browser or network. ARMORY_ROOT selects identical baseline/modified inputs.
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = process.env.ARMORY_ROOT || fileURLToPath(new URL('..', import.meta.url));
const { Controller } = await import(pathToFileURL(resolve(root, 'extension/controller.mjs')).href);
const { extractVisiblePage } = await import(pathToFileURL(resolve(root, 'extension/extract.mjs')).href);
const ARTICLE_URL = 'https://research.example.org/article';
const ARTICLE = '公开研究记录：本文介绍实验设计、数据来源、对照设置、观测结果和复现方法；每个结论都需要直接证据，并说明不确定性。'.repeat(15);

function fixture(t, { text = ARTICLE, tab = { id: 7, url: ARTICLE_URL }, password = false } = {}) {
  const names = ['chrome', 'fetch', 'Node', 'location', 'document', 'getComputedStyle'];
  const originals = Object.fromEntries(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const state = { session: {}, injections: 0, changes: 0, notices: [], forbidden: [] };
  const forbidden = name => async () => { state.forbidden.push(name); throw new Error(`UNEXPECTED_${name}`); };
  function element(tag, value = '') {
    return { nodeType: 1, tagName: tag, innerText: value, textContent: value,
      childNodes: value ? [{ nodeType: 3, textContent: value }] : [], parentElement: null,
      matches: () => false, getClientRects: () => [1], getAttribute: () => null,
      hasAttribute: () => false, id: '', className: '' };
  }
  const html = element('HTML'), body = element('BODY'), article = element('ARTICLE', text), paragraph = element('P', text);
  body.parentElement = html; article.parentElement = body; paragraph.parentElement = article;
  body.childNodes = [article]; article.childNodes = [paragraph];
  const input = element('INPUT'); input.parentElement = body;
  Object.defineProperty(input, 'value', { get() { throw new Error('PASSWORD_VALUE_READ'); } });
  const values = {
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 }, location: { href: ARTICLE_URL },
    fetch: forbidden('fetch'),
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1', contentVisibility: 'visible' }),
    document: { title: '本地研究正文', body, documentElement: html, querySelectorAll(selector) {
      if (selector.startsWith('input[')) return password ? [input] : [];
      if (selector.startsWith('article,')) return [article];
      if (selector.startsWith('h1,')) return [paragraph];
      return [];
    } },
    chrome: {
      storage: { session: { async set(value) { Object.assign(state.session, structuredClone(value)); } },
        local: { get: forbidden('storage.local.get'), set: forbidden('storage.local.set') } },
      permissions: { request: forbidden('permissions.request'), contains: forbidden('permissions.contains') },
      tabs: { async query(query) { assert.deepEqual(query, { active: true, currentWindow: true }); return tab ? [tab] : []; }, create: forbidden('tabs.create') },
      scripting: { async executeScript(options) {
        state.injections++;
        assert.equal(options.func, extractVisiblePage);
        assert.deepEqual(options.target, { tabId: 7, frameIds: [0] });
        assert.deepEqual(options.args, ['https://research.example.org', 20000]);
        assert.equal(options.world, 'ISOLATED');
        return [{ result: options.func(...options.args) }];
      } },
    },
  };
  for (const name of names) Object.defineProperty(globalThis, name, { value: values[name], configurable: true });
  t.after(() => {
    for (const name of names) {
      if (originals[name]) Object.defineProperty(globalThis, name, originals[name]);
      else delete globalThis[name];
    }
    assert.deepEqual(state.forbidden, [], 'local capture must not use network, permissions, persistent storage, or new tabs');
  });
  state.controller = new Controller({ onChange: () => state.changes++, onNotice: message => state.notices.push(message) });
  return state;
}

test('disconnected local capture extracts actual fixture text and stores only a session result', async t => {
  const state = fixture(t), controller = state.controller;
  assert.equal(controller.bridge.connected, false);
  await controller.captureLocal();
  assert.equal(state.injections, 1);
  assert.equal(controller.localPreview.text, ARTICLE);
  assert.equal(controller.localPreview.url, ARTICLE_URL);
  assert.equal(controller.localPreview.title, '本地研究正文');
  assert.match(controller.localPreview.markdown, /^# 本地研究正文\n\n公开研究记录/);
  assert.equal(controller.localPreview.quality, 'full');
  assert.equal(controller.localPreview.degraded, false);
  assert.deepEqual(state.session, { localPreview: controller.localPreview });
  assert.equal(controller.bridge.connected, false);
  assert.equal(state.changes, 1);
  assert.match(state.notices[0], /不上传中继/);
});

test('a long login wall is rejected rather than returned as captured article text', async t => {
  const state = fixture(t, { text: 'Sign in to continue reading. '.repeat(40), password: true });
  await assert.rejects(state.controller.captureLocal(), /登录墙|阅读限制/);
  assert.equal(state.injections, 1);
  assert.equal(state.controller.localPreview, null);
  assert.deepEqual(state.session, {});
});

test('missing active-tab access fails clearly before extraction or storage', async t => {
  const state = fixture(t, { tab: { id: 7 } });
  await assert.rejects(state.controller.captureLocal(), /激活网页并点击扩展图标/);
  assert.equal(state.injections, 0);
  assert.equal(state.controller.localPreview, null);
  assert.deepEqual(state.session, {});
});
