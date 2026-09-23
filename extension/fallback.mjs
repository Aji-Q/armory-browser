import { sameOrigin, validateResult } from './core.mjs';

export function usablePartialText(text) {
  const clean = text.replace(/\s+/g, ' ').trim();
  // Short legitimate passages are useful, but wall/challenge copy is never a result.
  return clean.length >= 80 && !/verify (?:that )?you are human|checking your browser|complete (?:the )?security check|access denied|captcha|subscribe to (?:continue|read|unlock)|subscription required|sign in to (?:continue|read)|log in to (?:continue|read)|验证您是人类|访问验证|验证码|订阅后(?:继续)?(?:阅读|查看)|登录后(?:继续)?(?:阅读|查看)/i.test(clean);
}

export async function anonymousFallback(job) {
  const target = new URL(job.url);
  if (/^(accounts\.google\.com|login\.microsoftonline\.com|login\.live\.com|appleid\.apple\.com|id\.apple\.com)$/.test(target.hostname) || /\.(auth0|okta|onelogin)\.com$/.test(target.hostname) || /(?:^|\/)(?:log-?in|sign-?in|oauth2?|sso|authorize|authentication)(?:\/|$)/i.test(target.pathname)) throw new Error('匿名回退不请求登录或身份认证页面');
  const timer = new AbortController();
  const timeout = setTimeout(() => timer.abort(), 20000);
  try {
    const response = await fetch(job.url, { credentials: 'omit', redirect: 'error', cache: 'no-store', referrerPolicy: 'no-referrer', signal: timer.signal, headers: { Accept: 'text/html,text/plain;q=0.9' } });
    if (!response.ok || !sameOrigin(response.url, job.url)) throw new Error(`匿名回退未取得原站正文（HTTP ${response.status}）`);
    const type = response.headers.get('content-type') || '';
    if (!/^(text\/html|text\/plain|application\/xhtml\+xml)\b/i.test(type)) throw new Error('匿名回退只接受 HTML 或纯文本，不解析二进制文件');
    const maxBytes = 1500000;
    if (Number(response.headers.get('content-length')) > maxBytes) throw new Error('匿名回退响应超过 1.5 MB 上限');
    const reader = response.body?.getReader();
    if (!reader) throw new Error('匿名回退没有响应正文');
    const decoder = new TextDecoder();
    let raw = '', bytes = 0;
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      bytes += chunk.value.byteLength;
      if (bytes > maxBytes) { await reader.cancel(); throw new Error('匿名回退响应超过 1.5 MB 上限'); }
      raw += decoder.decode(chunk.value, { stream: true });
    }
    raw += decoder.decode();
    let title = '', text = '', links = [];
    if (/^text\/plain\b/i.test(type)) text = raw.trim();
    else {
      // A detached template is inert: no script execution or subresource loading.
      // Server markup is never attached to the extension UI or browser document.
      const template = document.createElement('template');
      template.innerHTML = raw;
      const doc = template.content;
      title = (doc.querySelector('title')?.textContent || '').trim().slice(0, 1000);
      if (/^(?:sign in|log in|login|登录|身份验证)(?:\s|$)/i.test(title) || (doc.querySelector('input[type="password"]') && !doc.querySelector('article'))) throw new Error('匿名回退返回登录页面，不作为正文');
      doc.querySelectorAll('script,style,noscript,template,form,input,textarea,select,button,iframe,img,svg,canvas,video,audio,object,embed,nav,footer,[contenteditable],[hidden],[inert],[aria-hidden="true"],[style*="display:none"],[style*="display: none"],[style*="visibility:hidden"],[style*="visibility: hidden"]').forEach(node => node.remove());
      const root = doc.querySelector('article') || doc.querySelector('main,[role="main"]') || doc;
      const blocks = [...root.querySelectorAll('h1,h2,h3,p,li,blockquote,pre,td')].filter(node => !node.querySelector('p,li,blockquote,pre,td'));
      text = (blocks.length ? blocks.map(node => node.textContent.trim()).filter(Boolean).join('\n\n') : root.textContent).trim();
      links = [...root.querySelectorAll('a[href]')].slice(0, 200).flatMap(node => {
        try {
          const url = new URL(node.getAttribute('href'), job.url);
          const label = node.textContent.trim().slice(0, 1000);
          return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && label ? [{ text: label, url: url.href }] : [];
        } catch { return []; }
      });
    }
    if (!usablePartialText(text)) throw new Error('人工等待超时；匿名回退仍无可用正文或仅返回登录/验证墙。本任务失败，其他任务继续。');
    const truncated = text.length > job.max_chars;
    text = text.slice(0, job.max_chars);
    return validateResult({ url: response.url, title, text, markdown: (`# ${title}\n\n${text}`).slice(0, job.max_chars * 2), links, captured_at: new Date().toISOString(), truncated, degraded: true, quality: 'partial' }, job);
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('人工等待超时；匿名回退也在 20 秒后超时。本任务失败，其他任务继续。');
    throw error;
  } finally { clearTimeout(timeout); }
}
