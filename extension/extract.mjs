// Passed to chrome.scripting.executeScript as a fixed packaged function.
// This function is self-contained: no remote script, selector, or code is accepted.
export function extractVisiblePage(expectedOrigin, maxChars) {
  function safeSlice(value, limit) {
    // JSON/UTF-8 relay text must not contain isolated UTF-16 surrogates.
    const clean = value.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]|[\uD800-\uDFFF]/g, part => part.length === 2 ? part : '\uFFFD');
    const cut = clean.slice(0, limit);
    return /[\uD800-\uDBFF]$/.test(cut) ? cut.slice(0, -1) : cut;
  }
  const current = new URL(location.href);
  if (!['http:', 'https:'].includes(current.protocol) || current.username || current.password || current.origin !== expectedOrigin) return { humanRequired: true, reasonCode: 'origin_changed', reason: '页面已离开批准的 origin；请返回原网站。' };
  if (!Number.isInteger(maxChars) || maxChars < 100 || maxChars > 100000) throw new Error('Invalid extraction limit');
  const host = current.hostname.toLowerCase();
  const identityHost = /^(accounts\.google\.com|login\.microsoftonline\.com|login\.live\.com|appleid\.apple\.com|id\.apple\.com)$/.test(host) || /\.(auth0|okta|onelogin)\.com$/.test(host);
  const identityPath = /(?:^|\/)(?:log-?in|sign-?in|oauth2?|sso|authorize|authentication)(?:\/|$)/i.test(current.pathname);
  if (identityHost || identityPath) return { humanRequired: true, reasonCode: 'identity_page', reason: '当前是登录或身份认证页面。请手动登录并返回原正文页面；插件不会读取认证页面。' };

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
  if (!root) return { humanRequired: true, reasonCode: 'insufficient_content', reason: '页面尚无可见正文。请等待加载完成后恢复。' };
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
          const label = safeSlice((node.innerText || '').replace(/\s+/g, ' ').trim(), 1000);
          if (label) { links.push({ text: label, url: url.href }); seenLinks.add(url.href); }
        }
      } catch { /* Ignore non-web links. */ }
    }
    if (blocks.has(node.tagName)) { pieces.push('\n'); stack.push({ node, exit: true }); }
    for (let index = node.childNodes.length - 1; index >= 0; index--) stack.push({ node: node.childNodes[index], exit: false });
  }
  const raw = pieces.join('').replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  // Length is a payload bound, not evidence that an article is accessible. Check
  // explicit gate UI independently, even if a long teaser filled the payload cap.
  function inside(element, ancestor) {
    for (let node = element; node; node = node.parentElement) if (node === ancestor) return true;
    return false;
  }
  function quoted(element) {
    for (let node = element; node && node !== document.body; node = node.parentElement) {
      if (['BLOCKQUOTE', 'PRE', 'CODE', 'Q'].includes(node.tagName)) return true;
    }
    return false;
  }
  function gateLabel(element) {
    const pending = [element], words = [];
    let length = 0, nodes = 0;
    while (pending.length && length < 400 && ++nodes <= 1000) {
      const node = pending.pop();
      if (node.nodeType === Node.TEXT_NODE) {
        const part = node.textContent.slice(0, 400 - length);
        words.push(part); length += part.length;
      } else if (node.nodeType === Node.ELEMENT_NODE && visible(node)) {
        for (let index = node.childNodes.length - 1; index >= 0; index--) pending.push(node.childNodes[index]);
      }
    }
    return words.join(' ').trim();
  }
  function firstSentence(value) {
    return value.replace(/\s+/g, ' ').trim().slice(0, 320).split(/[.!?。！？]/, 1)[0].trim();
  }
  function gatePrompt(value) {
    const sentence = firstSentence(value);
    // Exact declarative restrictions are gates, even though they contain "is".
    // Anchor the whole sentence so an explanation of that message stays readable.
    if (/^(?:this|the) (?:article|content) is (?:(?:only )?for|(?:only )?available (?:only )?to) (?:paid )?(?:subscribers|members)(?: only)?$/i.test(sentence)) return true;
    // Explanatory prose such as `Subscribe to read is a common message` is not UI.
    if (/\b(?:is|are|was|were|means|describes|says|message|phrase|example|button|explains)\b|字样|提示语|示例|意味着/i.test(sentence)) return false;
    return /^(?:please\s+|you (?:need to|must|have to)\s+)?(?:sign[ -]?in|log[ -]?in|subscribe|register|create (?:an? )?account)(?:\s+or\s+(?:sign[ -]?in|log[ -]?in|subscribe|register))?\s+(?:to|and)\s+(?:continue(?:\s+reading)?|keep reading|read|view|access|unlock)\b/i.test(sentence)
      || /^(?:to |in order to )(?:continue reading|read (?:the |this )?(?:full )?article|access (?:the |this )?(?:full )?content)[, :]+(?:please )?(?:sign[ -]?in|log[ -]?in|subscribe|register)\b/i.test(sentence)
      || /^(?:subscription required|unlock this article|(?:this|the) (?:article|content) (?:is (?:only )?(?:for|available to) (?:paid )?subscribers|requires (?:an? )?(?:active )?(?:subscription|account)))(?:\b|$)/i.test(sentence)
      || /^(?:请先?|需要先?)?(?:登录|登入|注册|订阅|付费)(?:后|以|来|即可|才能)(?:继续)?(?:阅读|查看|访问|解锁)/.test(sentence);
  }
  function explicitlyRestrictsReading(value) {
    return /\b(?:read|reading|article|content|unlock|view)\b|阅读|查看|全文|文章|解锁/i.test(firstSentence(value));
  }
  let loginWall = false;
  let accessWall = false;
  let inspected = 0;
  // Inspect visible text blocks, not password values or form contents. Bounded
  // independently of maxChars so truncation cannot hide a trailing paywall notice.
  for (const element of document.querySelectorAll('h1,h2,h3,p,div,span,[role="dialog"]')) {
    if (++inspected > 50000) { capped = true; break; }
    if (!visibleThroughParents(element) || quoted(element)) continue;
    // Only leaf text blocks: a dialog/container's innerText could include a form.
    if (element.getAttribute('role') === 'dialog') continue;
    const label = gateLabel(element);
    const inRoot = inside(element, root);
    if (gatePrompt(label) && (inRoot || explicitlyRestrictsReading(label))) loginWall = true;
    const sentence = firstSentence(label);
    if (inRoot && ['H1', 'H2', 'H3'].includes(element.tagName)) {
      if (hasPassword && /^(?:sign[ -]?in|log[ -]?in|登录|登入)[ :]*$/i.test(sentence)) loginWall = true;
      if ((root.tagName !== 'ARTICLE' || hasChallenge) && /^(?:verify (?:that )?you are human|checking your browser|complete (?:the )?security check|access denied|验证您是人类|访问验证|完成安全验证)(?:\b|$)/i.test(sentence)) accessWall = true;
    }
  }
  if (gatePrompt(raw)) loginWall = true;
  if (loginWall) return { humanRequired: true, reasonCode: 'login_wall', reason: '检测到明确的登录墙或订阅阅读限制；长度充足的预览也不代表全文。请按网站要求手动取得访问权限后恢复。' };
  if (accessWall) return { humanRequired: true, reasonCode: 'access_challenge', reason: '检测到访问验证或拒绝访问页面，请手动处理；不会尝试绕过。' };
  // A nonblocking generic login/captcha popup must not interrupt usable content.
  const sufficient = raw.length >= Math.min(400, maxChars) && (root !== document.body || (raw.length >= 1000 && raw.split('\n').filter(line => line.trim().length > 50).length >= 4));
  if (!sufficient) {
    if (hasPassword || hasChallenge) return { humanRequired: true, reasonCode: hasPassword ? 'login_wall' : 'access_challenge', reason: hasPassword ? '正文不足且检测到登录界面，请手动处理后恢复。' : '正文不足且检测到验证码，请手动处理；不会绕过。' };
    if (/verify (?:that )?you are human|checking your browser|complete (?:the )?security check|access denied|验证您是人类|访问验证|完成安全验证/i.test(raw.slice(0, 4000))) return { humanRequired: true, reasonCode: 'access_challenge', reason: '正文不足且检测到访问验证或拒绝访问页面，请手动处理；不会尝试绕过。' };
    if (raw.split('\n').some(gatePrompt)) return { humanRequired: true, reasonCode: 'login_wall', reason: '正文不足且检测到登录墙或付费墙。请按网站要求手动取得访问权限后恢复。' };
    return { humanRequired: true, reasonCode: 'insufficient_content', reason: '可见正文不足，可能仍在加载或需人工操作。此版本不读取图片文字、PDF、跨域 iframe 或隐藏内容。' };
  }
  const text = safeSlice(raw, maxChars);
  const title = safeSlice(document.title.trim(), 1000);
  const markdown = safeSlice(`# ${title.replace(/[\r\n]+/g, ' ')}\n\n${text}`, maxChars * 2);
  return { result: { url: current.href, title, text, markdown, links, captured_at: new Date().toISOString(), truncated: capped || raw.length > maxChars, degraded: false, quality: 'full' } };
}
