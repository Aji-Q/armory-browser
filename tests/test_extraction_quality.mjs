import test from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
const rootPath = process.env.ARMORY_ROOT || fileURLToPath(new URL('..', import.meta.url));
const { extractVisiblePage } = await import(pathToFileURL(resolve(rootPath, 'extension/extract.mjs')).href);

// A deliberately small, visible-DOM fixture, not a browser acceptance test.
const text = value => ({ nodeType: 3, textContent: value });
function element(tag, children = [], attrs = {}) {
  if (typeof children === 'string') children = [text(children)];
  const node = { nodeType: 1, tagName: tag.toUpperCase(), childNodes: children, parentElement: null, attrs,
    id: attrs.id || '', className: attrs.class || '', hidden: !!attrs.hidden,
    getAttribute: name => attrs[name] ?? null, hasAttribute: name => name in attrs,
    getClientRects: () => attrs.hidden ? [] : [{}],
    matches(selector) { return selector.split(',').some(part => {
      part = part.trim();
      if (part === '[contenteditable]:not([contenteditable="false"])') return 'contenteditable' in attrs && attrs.contenteditable !== 'false';
      const attr = part.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
      if (attr) return attr[1] in attrs && (attr[2] === undefined || attrs[attr[1]] === attr[2]);
      return part.toUpperCase() === node.tagName;
    }); },
    contains(other) { for (let current = other; current; current = current.parentElement) if (current === node) return true; return false; },
    querySelectorAll(selector) { return descendants(node).filter(n => matches(n, selector)); },
    get innerText() { return children.map(child => child.nodeType === 3 ? child.textContent : child.hidden ? '' : child.innerText).join('\n'); }
  };
  children.forEach(child => { child.parentElement = node; });
  return node;
}
function descendants(node) { return node.childNodes.flatMap(child => child.nodeType === 1 ? [child, ...descendants(child)] : []); }
function matches(node, selector) {
  return selector.split(',').some(part => {
    part = part.trim();
    if (part === 'input[type="password"]') return node.tagName === 'INPUT' && node.attrs.type === 'password';
    if (part === 'iframe[src]') return node.tagName === 'IFRAME' && 'src' in node.attrs;
    if (part === '[id*="captcha"]') return node.id.includes('captcha');
    if (part === '[class*="captcha"]') return node.className.includes('captcha');
    return node.matches(part);
  });
}
function capture(children, options = {}) {
  const body = element('body', children), html = element('html', [body]);
  const old = Object.fromEntries(['document', 'location', 'Node', 'getComputedStyle'].map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const globals = {
    document: { title: options.title || 'Fixture', body, documentElement: html, querySelectorAll: selector => descendants(html).filter(node => matches(node, selector)) },
    location: { href: options.url || 'https://example.com/article' },
    Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
    getComputedStyle: node => ({ display: node.attrs.hidden ? 'none' : 'block', visibility: 'visible', opacity: '1', contentVisibility: 'visible' })
  };
  Object.entries(globals).forEach(([name, value]) => Object.defineProperty(globalThis, name, { value, configurable: true }));
  try { return extractVisiblePage(options.origin || 'https://example.com', options.maxChars || 20000); }
  finally { Object.entries(old).forEach(([name, descriptor]) => descriptor ? Object.defineProperty(globalThis, name, descriptor) : delete globalThis[name]); }
}
const paragraph = 'Open research observes the behavior of a well specified system and reports measured evidence. '.repeat(12);
const article = (...extra) => element('article', [element('h1', 'Research report'), element('p', paragraph), ...extra]);
function isWall(result, code = 'login_wall') { assert.equal(result.humanRequired, true); assert.equal(result.result, undefined); assert.equal(result.reasonCode, code); }
function isFull(result) { assert.equal(result.humanRequired, undefined); assert.equal(result.result.quality, 'full'); }

test('1030+ character main login wall is not a full article', () => {
  isWall(capture([element('main', [element('h1', 'Sign in to continue'), element('p', 'Sign in to continue. Subscribe to read this article. Access requires an active account. '.repeat(14)), element('form', [element('input', [], { type: 'password' })])])]));
});
test('long subscription marketing wall without password is not a full article', () => {
  isWall(capture([element('main', [element('h1', 'Subscribe to read this article'), element('p', paragraph.repeat(3))])]));
});
test('long article teaser plus trailing explicit paywall is not full', () => {
  isWall(capture([article(element('p', 'Subscribe to continue reading this article.'))]));
});
test('outside-article dialog explicitly restricting further reading is a paywall', () => {
  isWall(capture([article(), element('div', [element('h2', 'Sign in to read the full article')], { role: 'dialog' })]));
});
test('gate detection is not bypassed by a small payload limit', () => {
  isWall(capture([article(element('p', paragraph.repeat(10)), element('p', 'Subscribe to continue reading.'))], { maxChars: 100 }));
});
test('Chinese long teaser with required login is not full', () => {
  isWall(capture([article(element('p', '登录后继续阅读完整文章。'))]));
});
test('long main access verification wall is not full', () => {
  isWall(capture([element('main', [element('h1', 'Verify you are human'), element('p', paragraph), element('div', [], { id: 'captcha' })])]), 'access_challenge');
});
test('visible authentication-only heading with password is not full', () => {
  isWall(capture([element('main', [element('h1', 'Sign in'), element('p', paragraph), element('form', [element('input', [], { type: 'password' })])])]));
});
test('complete article ignores unrelated generic login overlay and captcha widget', () => {
  isFull(capture([article(), element('div', [element('h2', 'Sign in'), element('form', [element('input', [], { type: 'password' })]), element('div', [], { id: 'captcha' })], { role: 'dialog' })]));
});
test('complete article ignores nonblocking comment login prompt', () => {
  isFull(capture([article(), element('div', [element('h2', 'Sign in to comment')], { role: 'dialog' })]));
});
test('article discussing login, captcha and quoted restriction messages is readable', () => {
  isFull(capture([element('article', [element('h1', 'How login and CAPTCHA work'), element('p', 'The phrase "Sign in to continue" is common in authentication UI. '.repeat(18)), element('h2', 'Login wall design'), element('p', 'Subscribe to read is a common message, not a restriction on this article. '.repeat(8))])]));
});
test('quotation blocks describing exact prompt are not active gates', () => {
  isFull(capture([article(element('blockquote', [element('p', 'Subscribe to read this article.')]))]));
});
test('hidden wall hints do not downgrade accessible article', () => {
  isFull(capture([article(element('div', [element('h2', 'Sign in to continue reading')], { hidden: true }))]));
});
test('forms and hidden inputs are not returned', () => {
  const result = capture([article(element('form', [element('p', 'PRIVATE FORM SECRET'), element('input', [], { type: 'password' })]), element('div', 'HIDDEN SECRET', { hidden: true }))]);
  isFull(result); assert.equal(result.result.text.includes('SECRET'), false);
});
test('short loading shell is retryable insufficient content, not a confirmed wall', () => {
  isWall(capture([element('main', [element('p', 'Loading…')])]), 'insufficient_content');
});
test('identity and origin changes have non-retryable reason codes', () => {
  isWall(capture([article()], { url: 'https://example.com/login' }), 'identity_page');
  isWall(capture([article()], { url: 'https://other.example/article' }), 'origin_changed');
});
test('normal result remains bounded and excludes secret form content', () => {
  const result = capture([article()], { maxChars: 100 });
  isFull(result); assert.equal(result.result.text.length, 100); assert.equal(result.result.truncated, true);
});

test('plain main text and leaf div wall prompts are detected without headings', () => {
  isWall(capture([element('main', 'Sign in to continue. '.repeat(60))]));
  isWall(capture([article(element('div', [element('span', 'Subscribe to continue reading.')]))]));
});
test('gate scan does not inspect form text or hidden inline text', () => {
  isFull(capture([article(element('div', [element('form', [element('p', 'Subscribe to continue reading.')])]), element('p', [element('span', 'Subscribe to continue reading.', { 'aria-hidden': 'true' }), text('Available public text.')]))]));
});

test('explicit subscriber-only declarative prompts cannot be masked by the word is', () => {
  for (const prompt of ['This article is only for subscribers.', 'This content is available to subscribers.', 'This content is available only to paid subscribers.', 'This article is only available to subscribers.', 'The article is for subscribers only.']) isWall(capture([article(element('h2', prompt))]));
});
test('you need or must sign in to read is an active gate', () => {
  for (const prompt of ['You need to sign in to continue reading.', 'You must log in to read the full article.']) isWall(capture([article(element('p', prompt))]));
});
test('subscriber-only statements in quotes and explanations are not gates', () => {
  isFull(capture([article(element('blockquote', [element('p', 'This article is only for subscribers.')]), element('p', 'This content is available to subscribers is an example of a restrictive message.'))]));
});

test('text, title, markdown and link bounds never split a surrogate pair', () => {
  const result=capture([element('article',[element('p','A'.repeat(99)+'😀'+'Z'.repeat(600)),element('a','L'.repeat(999)+'😀end',{href:'https://example.org/a'})])],{maxChars:100,title:'T'.repeat(199)+'😀'+'X'.repeat(798)+'😀end'});
  isFull(result);
  for (const value of [result.result.text,result.result.title,result.result.markdown,...result.result.links.map(x=>x.text)]) assert.equal(value.isWellFormed(),true);
  assert.ok(result.result.text.length<=100); assert.equal(result.result.truncated,true);
});
