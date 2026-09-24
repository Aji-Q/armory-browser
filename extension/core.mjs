// Pure validation/state functions: no Chrome API, DOM, storage, or network access.
export const STATES = Object.freeze(['queued', 'running', 'awaiting_human', 'awaiting_share', 'completed', 'failed', 'cancelled']);
export const TERMINAL = Object.freeze(['completed', 'failed', 'cancelled']);
const TRANSITIONS = Object.freeze({ approve: ['queued', 'running'], human_required: ['running', 'awaiting_human'], resume: ['awaiting_human', 'running'], timeout: ['awaiting_human', 'running'], preview_ready: ['running', 'awaiting_share'], complete: ['awaiting_share', 'completed'] });
// Slice by the existing UTF-16 limit without splitting a supplementary Unicode
// character. Existing malformed code units are normalized before relay transfer.
export function safeSlice(value, limit) {
  const sliced = value.slice(0, limit);
  return /[\uD800-\uDBFF]$/.test(sliced) ? sliced.slice(0, -1) : sliced;
}
const wellFormed = value => value.replace(/[\uD800-\uDFFF]/gu, '\uFFFD');
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isLoopback(hostname) {
  return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]';
}

export function webURL(value) {
  if (typeof value !== 'string' || !value || value.length > 8192 || /[\s\\\u0000-\u001f\u007f\uD800-\uDFFF]/u.test(value)) throw new Error('URL 格式无效');
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || /^[^:]+:\/\/[^/]*@/.test(value)) throw new Error('只接受不含凭据的 HTTP(S) URL');
  return url;
}

export function relayURL(value) {
  const url = webURL(value.trim());
  if (url.protocol !== 'https:' && !isLoopback(url.hostname)) throw new Error('中继必须使用 HTTPS；仅本机开发可用 HTTP');
  if (url.search || url.hash) throw new Error('中继 URL 不得包含查询参数或片段');
  return url.href.replace(/\/+$/, '');
}

// Keep this set aligned with bridge/relay.py SENSITIVE_QUERY_KEYS. Filtering an
// ancillary link is preferable to making an otherwise valid article unshareable.
const SENSITIVE_QUERY_KEYS = new Set(['access_token', 'refresh_token', 'id_token', 'token', 'auth_token', 'api_key',
  'apikey', 'password', 'passwd', 'secret', 'client_secret', 'session_token', 'authorization', 'cookie', 'set-cookie',
  'auth', 'session', 'sessionid', 'sid', 'jwt', 'bearer', 'signature', 'x-amz-signature', 'x-amz-credential',
  'x-goog-signature', 'x-goog-credential']);

export function remoteURL(value) {
  const url = webURL(value);
  if (url.port === '0') throw new Error('远程 URL 端口无效');
  const host = url.hostname.toLowerCase().replace(/\.+$/, '');
  if (!host || host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local') || host.endsWith('.internal')) throw new Error('远程任务不可访问本机或私有网络地址');
  const ipv4 = /^\d+\.\d+\.\d+\.\d+$/.test(host) ? host.split('.').map(Number) : null;
  if (ipv4) {
    const [a, b, c] = ipv4;
    // Conservative public-address subset: private, shared, loopback, link-local,
    // documentation, benchmarking, unspecified and reserved/multicast are out.
    const blocked = a === 0 || a === 10 || a === 127 || a >= 224 ||
      (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && ((b === 0 && (c === 0 || c === 2)) || b === 168)) ||
      (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) || (a === 203 && b === 0 && c === 113);
    if (blocked) throw new Error('远程 URL 不是公开网络地址');
  } else if (host.startsWith('[')) {
    const address = host.slice(1, -1), groups = address.split(':');
    // Public global unicast only, excluding IETF special assignments, 6to4 and
    // documentation ranges. IPv4-mapped/ULA/link-local/zone IDs never pass.
    const special2001 = groups[0] === '2001' && ((parseInt(groups[1] || '0', 16) < 0x200) || groups[1] === 'db8');
    if (!/^[23][0-9a-f]{3}:/.test(address) || special2001 || groups[0] === '2002' || /^3fff:/.test(address)) throw new Error('远程 URL 不是公开 IPv6 地址');
  } else {
    const labels = host.split('.');
    if (host.length > 253 || labels.length < 2 || !labels.every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) throw new Error('远程 URL 主机名无效');
  }
  if ([...url.searchParams.keys()].some(key => SENSITIVE_QUERY_KEYS.has(key.toLowerCase()))) throw new Error('URL 查询参数包含凭据字段，拒绝回传');
  return url;
}

// Deliberately limited canonicalization: exact origin and query, with only a
// trailing slash and a fragment treated as equivalent. Never infer that an
// account home page or a same-origin redirect is the requested article.
export function sameResource(a, b) {
  try {
    const left = webURL(a), right = webURL(b);
    const path = url => url.pathname.replace(/\/+$/, '') || '/';
    return left.origin === right.origin && path(left) === path(right) && left.search === right.search;
  } catch { return false; }
}

export function permissionPattern(value) {
  const url = webURL(value);
  // Chrome host permissions do not scope ports. Execution separately enforces exact origin.
  return `${url.protocol}//${url.hostname}/*`;
}

export function sameOrigin(a, b) {
  try { return webURL(a).origin === webURL(b).origin; } catch { return false; }
}

export function validateJob(value, previous) {
  if (!value || typeof value !== 'object' || !UUID.test(value.id)) throw new Error('任务 ID 无效');
  const url = remoteURL(value.url).href;
  if (!STATES.includes(value.state)) throw new Error('任务状态无效');
  if (typeof value.purpose !== 'string' || value.purpose.length > 500) throw new Error('任务目的无效');
  if (!Number.isInteger(value.max_chars) || value.max_chars < 100 || value.max_chars > 100000) throw new Error('正文长度上限无效');
  if (previous && (previous.id !== value.id || previous.url !== url || previous.max_chars !== value.max_chars || previous.purpose !== value.purpose)) throw new Error('同一任务的采集范围发生变化；拒绝执行');
  // Deliberately omit remote result, script, selectors, and any unknown properties.
  if (value.human_timeout_seconds !== undefined && (!Number.isInteger(value.human_timeout_seconds) || value.human_timeout_seconds < 5 || value.human_timeout_seconds > 900)) throw new Error('人工等待期限无效');
  return { id: value.id, url, purpose: value.purpose, max_chars: value.max_chars, state: value.state, reason: typeof value.reason === 'string' ? value.reason.slice(0, 1000) : '', created_at: safeDate(value.created_at), updated_at: safeDate(value.updated_at), human_deadline_at: safeDate(value.human_deadline_at), human_timeout_seconds: value.human_timeout_seconds || 300, degraded: value.degraded === true };
}

function safeDate(value) {
  return typeof value === 'string' && Number.isFinite(Date.parse(value)) ? new Date(value).toISOString() : '';
}

export function nextState(state, event) {
  if (['fail', 'cancel'].includes(event) && STATES.includes(state) && !TERMINAL.includes(state)) return event === 'fail' ? 'failed' : 'cancelled';
  const transition = TRANSITIONS[event];
  if (!transition || transition[0] !== state) throw new Error(`不允许的任务转换：${state} → ${event}`);
  return transition[1];
}

export function validateResult(value, job) {
  if (!value || typeof value !== 'object' || !sameResource(value.url, job.url)) throw new Error('结果必须来自原任务页面，不能以同 origin 的其他页面冒充正文');
  const url = (job.id ? remoteURL(value.url) : webURL(value.url)).href;
  if (typeof value.title !== 'string' || value.title.length > 1000 || typeof value.text !== 'string' || !value.text.trim() || value.text.length > job.max_chars || typeof value.markdown !== 'string' || value.markdown.length > job.max_chars * 2 || !Array.isArray(value.links) || value.links.length > 200 || typeof value.truncated !== 'boolean' || !safeDate(value.captured_at)) throw new Error('采集结果字段无效');
  const links = value.links.flatMap(link => {
    try {
      if (!link || typeof link.text !== 'string' || link.text.length > 1000) return [];
      return [{ text: wellFormed(link.text), url: remoteURL(link.url).href }];
    } catch { return []; } // Drop unsafe secondary links, never the valid body.
  });
  if (value.degraded !== undefined && typeof value.degraded !== 'boolean') throw new Error('降级标记无效');
  if (value.quality !== undefined && !['full', 'partial'].includes(value.quality)) throw new Error('质量标记无效');
  return { url, title: wellFormed(value.title), text: wellFormed(value.text), markdown: wellFormed(value.markdown), links, captured_at: value.captured_at, truncated: value.truncated, degraded: value.degraded === true, quality: value.quality || (value.degraded ? 'partial' : 'full') };
}

export function boundedResult(value, job, byteLimit = 550000) {
  const result = validateResult(value, job);
  const encoder = new TextEncoder();
  const size = () => encoder.encode(JSON.stringify(result)).byteLength;
  while (result.links.length && size() > byteLimit) { result.links.pop(); result.truncated = true; }
  while (size() > byteLimit && (result.text.length || result.markdown.length)) {
    result.text = safeSlice(result.text, Math.floor(result.text.length * 0.8));
    result.markdown = safeSlice(result.markdown, Math.floor(result.markdown.length * 0.8));
    result.truncated = true;
  }
  if (size() > byteLimit || !result.text.trim()) throw new Error('结果元数据超过上传字节限制，或已无法保留非空正文');
  return result;
}

export function persistentEntry(job, local = {}) {
  // Whitelist only task metadata and tab linkage. Never persist page content or tokens.
  return { ...validateJob(job), tabId: Number.isInteger(local.tabId) ? local.tabId : null, localError: typeof local.localError === 'string' ? local.localError.slice(0, 1000) : '', approved: local.approved === true, humanDeadline: Number.isFinite(local.humanDeadline) ? local.humanDeadline : null, timeoutFallback: local.timeoutFallback === true, ownedTab: local.ownedTab === true, hadHuman: local.hadHuman === true, tabClosed: local.tabClosed === true, localUpdatedAt: Number.isFinite(local.localUpdatedAt) ? local.localUpdatedAt : 0, localTerminal: local.localTerminal === true };
}

export function canCapture(job, tab, local) {
  if (!local?.approved || !Number.isInteger(local.tabId) || tab?.id !== local.tabId) return { allowed: false, reason: '没有本地批准的专属标签页；请取消并重新创建任务' };
  if (!sameOrigin(tab.url, job.url)) return { allowed: false, reason: '标签页已离开原任务 origin。请手动完成登录并返回原网站后再恢复；不会提取身份提供商页面。' };
  if (!sameResource(tab.url, job.url)) return { allowed: false, reason: '标签页不是原任务页面（路径或查询参数已改变）。请点恢复以返回原任务 URL；不会把账户首页当作文章。' };
  if (!['running', 'awaiting_human', 'awaiting_share'].includes(job.state)) return { allowed: false, reason: '当前任务状态不允许采集' };
  return { allowed: true, reason: '' };
}

export const GRANT_TTL_MS = 8 * 60 * 60 * 1000;
export const HUMAN_TIMEOUT_MS = 300 * 1000;

export function makeGrant(relay, originURL, now = Date.now()) {
  return { relay: relayURL(relay), origin: remoteURL(originURL).origin, createdAt: now, expiresAt: now + GRANT_TTL_MS, autoShare: true };
}

export function hasGrant(grants, relay, jobURL, now = Date.now()) {
  try {
    const origin = remoteURL(jobURL).origin;
    const grant = grants?.[origin];
    return Boolean(grant && grant.relay === relayURL(relay) && grant.origin === origin && grant.autoShare === true && Number.isFinite(grant.createdAt) && Number.isFinite(grant.expiresAt) && grant.createdAt <= now && grant.expiresAt > now && grant.expiresAt - grant.createdAt <= GRANT_TTL_MS);
  } catch { return false; }
}

export function humanDeadline(job, now = Date.now()) {
  if (job.human_deadline_at && Number.isFinite(Date.parse(job.human_deadline_at))) return Date.parse(job.human_deadline_at);
  if (Number.isFinite(job.humanDeadline)) return job.humanDeadline;
  const updated = Date.parse(job.updated_at);
  return (Number.isFinite(updated) ? updated : now) + (job.human_timeout_seconds || 300) * 1000;
}

export function nextAutomaticAction(job, grants, relay, enabled, now = Date.now()) {
  if (job.localTerminal || !enabled || !hasGrant(grants, relay, job.url, now)) return null;
  if (job.state === 'queued') return 'approve_capture';
  if (job.state === 'running') return job.timeoutFallback || job.degraded ? 'anonymous_fallback' : 'capture';
  if (job.state === 'awaiting_human' && now >= humanDeadline(job, now)) return 'timeout_fallback';
  if (job.state === 'awaiting_share') return 'share';
  return null;
}
