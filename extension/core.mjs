// Pure validation/state functions: no Chrome API, DOM, storage, or network access.
export const STATES = Object.freeze(['queued', 'running', 'awaiting_human', 'awaiting_share', 'completed', 'failed', 'cancelled']);
export const TERMINAL = Object.freeze(['completed', 'failed', 'cancelled']);
const TRANSITIONS = Object.freeze({ approve: ['queued', 'running'], human_required: ['running', 'awaiting_human'], resume: ['awaiting_human', 'running'], timeout: ['awaiting_human', 'running'], preview_ready: ['running', 'awaiting_share'], complete: ['awaiting_share', 'completed'] });
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isLoopback(hostname) {
  return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]';
}

export function webURL(value) {
  if (typeof value !== 'string' || value.length > 8192) throw new Error('URL 格式无效');
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('只接受不含凭据的 HTTP(S) URL');
  return url;
}

export function relayURL(value) {
  const url = webURL(value.trim());
  if (url.protocol !== 'https:' && !isLoopback(url.hostname)) throw new Error('中继必须使用 HTTPS；仅本机开发可用 HTTP');
  if (url.search || url.hash) throw new Error('中继 URL 不得包含查询参数或片段');
  return url.href.replace(/\/+$/, '');
}

export function remoteURL(value) {
  const url = webURL(value);
  const host = url.hostname.toLowerCase();
  const localName = host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local') || host.endsWith('.internal') || (!host.includes('.') && !host.includes(':'));
  const ipv4 = /^\d+\.\d+\.\d+\.\d+$/.test(host) ? host.split('.').map(Number) : null;
  const private4 = ipv4 && (ipv4[0] === 0 || ipv4[0] === 10 || ipv4[0] === 127 || ipv4[0] >= 224 || (ipv4[0] === 169 && ipv4[1] === 254) || (ipv4[0] === 172 && ipv4[1] >= 16 && ipv4[1] <= 31) || (ipv4[0] === 192 && ipv4[1] === 168) || (ipv4[0] === 100 && ipv4[1] >= 64 && ipv4[1] <= 127));
  // Public unicast IPv6 only; mapped IPv4, ULA, link-local, multicast are rejected.
  const private6 = host.startsWith('[') && !/^\[[23][0-9a-f]{0,3}:/.test(host);
  if (localName || private4 || private6) throw new Error('远程任务不可访问本机或私有网络地址');
  return url;
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
  if (!value || typeof value !== 'object' || !sameOrigin(value.url, job.url)) throw new Error('结果必须来自批准的同一 origin');
  const url = webURL(value.url).href;
  if (typeof value.title !== 'string' || value.title.length > 1000 || typeof value.text !== 'string' || value.text.length > job.max_chars || typeof value.markdown !== 'string' || value.markdown.length > job.max_chars * 2 || !Array.isArray(value.links) || value.links.length > 200 || typeof value.truncated !== 'boolean' || !safeDate(value.captured_at)) throw new Error('采集结果字段无效');
  const links = value.links.map(link => {
    if (!link || typeof link.text !== 'string' || link.text.length > 1000) throw new Error('链接字段无效');
    return { text: link.text, url: webURL(link.url).href };
  });
  if (value.degraded !== undefined && typeof value.degraded !== 'boolean') throw new Error('降级标记无效');
  if (value.quality !== undefined && !['full', 'partial'].includes(value.quality)) throw new Error('质量标记无效');
  return { url, title: value.title, text: value.text, markdown: value.markdown, links, captured_at: value.captured_at, truncated: value.truncated, degraded: value.degraded === true, quality: value.quality || (value.degraded ? 'partial' : 'full') };
}

export function boundedResult(value, job, byteLimit = 550000) {
  const result = validateResult(value, job);
  const encoder = new TextEncoder();
  const size = () => encoder.encode(JSON.stringify(result)).byteLength;
  while (result.links.length && size() > byteLimit) { result.links.pop(); result.truncated = true; }
  while (size() > byteLimit && (result.text.length || result.markdown.length)) {
    result.text = result.text.slice(0, Math.floor(result.text.length * 0.8));
    result.markdown = result.markdown.slice(0, Math.floor(result.markdown.length * 0.8));
    result.truncated = true;
  }
  if (size() > byteLimit) throw new Error('结果元数据超过上传字节限制');
  return result;
}

export function persistentEntry(job, local = {}) {
  // Whitelist only task metadata and tab linkage. Never persist page content or tokens.
  return { ...validateJob(job), tabId: Number.isInteger(local.tabId) ? local.tabId : null, localError: typeof local.localError === 'string' ? local.localError.slice(0, 1000) : '', approved: local.approved === true, humanDeadline: Number.isFinite(local.humanDeadline) ? local.humanDeadline : null, timeoutFallback: local.timeoutFallback === true, ownedTab: local.ownedTab === true, hadHuman: local.hadHuman === true, tabClosed: local.tabClosed === true, localUpdatedAt: Number.isFinite(local.localUpdatedAt) ? local.localUpdatedAt : 0 };
}

export function canCapture(job, tab, local) {
  if (!local?.approved || !Number.isInteger(local.tabId) || tab?.id !== local.tabId) return { allowed: false, reason: '没有本地批准的专属标签页；请取消并重新创建任务' };
  if (!sameOrigin(tab.url, job.url)) return { allowed: false, reason: '标签页已离开原任务 origin。请手动完成登录并返回原网站后再恢复；不会提取身份提供商页面。' };
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
  if (!enabled || !hasGrant(grants, relay, job.url, now)) return null;
  if (job.state === 'queued') return 'approve_capture';
  if (job.state === 'running') return job.timeoutFallback || job.degraded ? 'anonymous_fallback' : 'capture';
  if (job.state === 'awaiting_human' && now >= humanDeadline(job, now)) return 'timeout_fallback';
  if (job.state === 'awaiting_share') return 'share';
  return null;
}
