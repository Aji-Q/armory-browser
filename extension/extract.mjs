// Passed to chrome.scripting.executeScript as a fixed packaged function.
// This function is self-contained: no remote script, selector, or code is accepted.
export function extractVisiblePage(expectedOrigin, maxChars) {
  const current = new URL(location.href);
  if (!['http:', 'https:'].includes(current.protocol) || current.username || current.password || current.origin !== expectedOrigin) return { humanRequired: true, reason: '页面已离开批准的 origin；请返回原网站。' };
  if (!Number.isInteger(maxChars) || maxChars < 100 || maxChars > 100000) throw new Error('Invalid extraction limit');
  const host = current.hostname.toLowerCase();
  const identityHost = /^(accounts\.google\.com|login\.microsoftonline\.com|login\.live\.com|appleid\.apple\.com|id\.apple\.com)$/.test(host) || /\.(auth0|okta|onelogin)\.com$/.test(host);
  const identityPath = /(?:^|\/)(?:log-?in|sign-?in|oauth2?|sso|authorize|authentication)(?:\/|$)/i.test(current.pathname);
  if (identityHost || identityPath) return { humanRequired: true, reason: '当前是登录或身份认证页面。请手动登录并返回原正文页面；插件不会读取认证页面。' };

  const skipped = 'script,style,noscript,template,form,input,textarea,select,option,button,[contenteditable]:not([contenteditable="false"]),[hidden],[inert],[aria-hidden="true"]';
  function visible(element) {
    if (!element || element.nodeType !== Node.ELEMENT_NODE || element.matches(skipped)) return false;
    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || style.opacity === '0' || style.contentVisibility === 'hidden') return false;
    if (style.clip === 'rect(0px, 0px, 0px, 0px)' || style.clipPath === 'inset(50%)') return false;
    return style.display === 'contents' || element.getClientRects().length > 0;
  }
  function visibleThroughParents(element) {
    for (let node = element; node && node !== document.documentElement; node = node.parentElement) if (!visible(node)) return false;
    return true;
  }
  const hasPassword = [...document.querySelectorAll('input[type="password"]')].some(element => {
    // Only inspect the element's presence/layout, never its value or form contents.
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && element.getClientRects().length > 0;
  });
  const hasChallenge = [...document.querySelectorAll('iframe[src], [id*="captcha"], [class*="captcha"], [data-sitekey]')].some(element => visibleThroughParents(element) && (/captcha|challenges\.cloudflare/i.test(element.getAttribute('src') || '') || /captcha/i.test(element.id + ' ' + element.className) || element.hasAttribute('data-sitekey')));
  const candidates = [...document.querySelectorAll('article, main, [role="main"]')].filter(visibleThroughParents);
  // Prefer article, then main. Empty shells fall back to visible body only.
  const root = candidates.find(element => element.tagName === 'ARTICLE' && element.innerText.trim().length > 80) || candidates.find(element => element.innerText.trim().length > 80) || document.body;
  if (!root) return { humanRequired: true, reason: '页面尚无可见正文。请等待加载完成后恢复。' };
  const pieces = [];
  const links = [];
  const seenLinks = new Set();
  const stack = [{ node: root, exit: false }];
  let chars = 0;
  let visited = 0;
  let capped = false;
  const blocks = new Set(['P', 'DIV', 'SECTION', 'ARTICLE', 'MAIN', 'HEADER', 'FOOTER', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LI', 'TR', 'BLOCKQUOTE', 'PRE']);
  while (stack.length) {
    if (++visited > 50000 || chars > maxChars + 4000) { capped = true; break; }
    const entry = stack.pop();
    const node = entry.node;
    if (entry.exit) { pieces.push('\n'); continue; }
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent.replace(/\s+/g, ' ');
      pieces.push(text); chars += text.length;
      continue;
    }
    if (node.nodeType !== Node.ELEMENT_NODE || !visible(node)) continue;
    if (node.tagName === 'BR') { pieces.push('\n'); continue; }
    if (node.tagName === 'A' && links.length < 200) {
      try {
        const url = new URL(node.getAttribute('href'), location.href);
        if (node.hasAttribute('href') && ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && !seenLinks.has(url.href)) {
          // The label must come from visible text, not hidden child textContent.
          const label = (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 1000);
          if (label) { links.push({ text: label, url: url.href }); seenLinks.add(url.href); }
        }
      } catch { /* Ignore non-web links. */ }
    }
    if (blocks.has(node.tagName)) { pieces.push('\n'); stack.push({ node, exit: true }); }
    for (let index = node.childNodes.length - 1; index >= 0; index--) stack.push({ node: node.childNodes[index], exit: false });
  }
  const raw = pieces.join('').replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  const headings = [...document.querySelectorAll('h1,h2,[role="dialog"]')].filter(visibleThroughParents).map(element => element.innerText || '').join(' ').slice(0, 4000);
  const wallText = `${headings}\n${raw.slice(0, 4000)}`;
  // A nonblocking login/captcha popup must not interrupt usable article content.
  const sufficient = raw.length >= Math.min(400, maxChars) && (root !== document.body || (raw.length >= 1000 && raw.split('\n').filter(line => line.trim().length > 50).length >= 4));
  if (!sufficient) {
    if (hasPassword || hasChallenge) return { humanRequired: true, reason: hasPassword ? '正文不足且检测到登录界面，请手动处理后恢复。' : '正文不足且检测到验证码，请手动处理；不会绕过。' };
    if (/verify (?:that )?you are human|checking your browser|complete (?:the )?security check|access denied|验证您是人类|访问验证|完成安全验证/i.test(wallText)) return { humanRequired: true, reason: '正文不足且检测到访问验证或拒绝访问页面，请手动处理；不会尝试绕过。' };
    if (/subscribe to (?:continue|read|unlock)|subscription required|unlock this article|sign in to (?:continue|read)|log in to (?:continue|read)|订阅后(?:继续)?(?:阅读|查看)|付费后(?:阅读|查看)|登录后(?:继续)?(?:阅读|查看)/i.test(wallText)) return { humanRequired: true, reason: '正文不足且检测到登录墙或付费墙。请按网站要求手动取得访问权限后恢复。' };
    return { humanRequired: true, reason: '可见正文不足，可能仍在加载或需人工操作。此版本不读取图片文字、PDF、跨域 iframe 或隐藏内容。' };
  }
  const text = raw.slice(0, maxChars);
  const title = document.title.trim().slice(0, 1000);
  const markdown = (`# ${title.replace(/[\r\n]+/g, ' ')}\n\n${text}`).slice(0, maxChars * 2);
  return { result: { url: current.href, title, text, markdown, links, captured_at: new Date().toISOString(), truncated: capped || raw.length > maxChars, degraded: false, quality: 'full' } };
}
