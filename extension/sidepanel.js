import { Controller } from './controller.mjs';
import { hasGrant, humanDeadline, TERMINAL } from './core.mjs';

const $ = id => document.getElementById(id);
const labels = { queued: '等待站点授权', running: '自动采集中', awaiting_human: '等待人工', awaiting_share: '待自动回传', completed: '已回传', failed: '失败 · 其他任务继续', cancelled: '已取消' };
const controller = new Controller({ onChange: render, onNotice: notice });
const emptyContent = Object.fromEntries(['jobs-list', 'human-list', 'preview-list'].map(id => [id, $(id).firstElementChild.cloneNode(true)]));

function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function button(label, action, id, secondary = true) {
  const node = element('button', secondary ? 'secondary' : '', label);
  node.type = 'button'; node.dataset.action = action; if (id) node.dataset.id = id;
  node.disabled = Boolean(id && controller.busy.has(id));
  return node;
}
function card(job) {
  const node = element('article', 'job-card');
  const authorized = hasGrant(controller.grants, controller.bridge.url, job.url);
  const stateLabel = job.state === 'queued' && authorized ? (controller.autoEnabled ? '排队自动处理' : '自动处理已暂停') : labels[job.state];
  node.append(element('span', 'local-tag', stateLabel), element('h3', '', new URL(job.url).hostname), element('code', 'job-url', job.url), element('p', 'job-purpose', job.purpose));
  if (job.reason) node.append(element('p', 'job-reason', job.reason));
  if (job.localError) node.append(element('p', 'job-reason', job.localError));
  const actions = element('div', 'actions');
  if (!authorized && !TERMINAL.includes(job.state)) {
    node.append(element('p', 'privacy-note', `授权后，此 origin 的后续任务在本会话（最长 8 小时）自动打开后台页、读取可见正文并回传到 ${controller.bridge.url}。不逐任务询问；可随时撤销。Chrome 网站权限不区分端口，插件额外限制精确 origin。`));
    actions.append(button('授权此站点自动采集与回传 · 8h', 'authorize', job.id, false));
  }
  if (job.state === 'awaiting_human') {
    const seconds = Math.max(0, Math.ceil((humanDeadline(job) - Date.now()) / 1000));
    node.append(element('p', 'privacy-note', seconds ? `人工处理剩余 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}。截止后自动匿名回退，不读取认证页面。` : '等待期限已到：恢复连接/授权后自动匿名回退，结果仅按部分正文返回。'));
    if (authorized && controller.autoEnabled && seconds > 0) actions.append(button('已处理，恢复采集', 'resume', job.id, false));
  }
  if (job.tabId) actions.append(button('打开专属页面', 'open', job.id));
  if (!TERMINAL.includes(job.state)) actions.append(button('取消任务', 'cancel', job.id));
  if (actions.childElementCount) node.append(actions);
  node.append(element('p', 'job-footer', `任务 ${job.id.slice(0, 8)}${job.degraded ? ' · 降级回退' : ''}`));
  return node;
}
function previewCard(result, job = null) {
  const node = element('article', 'job-card');
  const label = job ? `${labels[job.state]}${result.degraded ? ' · 部分正文' : ''}` : '仅本地 · 不上传';
  node.append(element('span', 'local-tag', label), element('h3', '', result.title || '无标题页面'), element('code', 'job-url', result.url));
  node.append(element('p', 'job-footer', `${result.text.length.toLocaleString()} 字符 · ${result.links.length} 个链接${result.truncated ? ' · 已截断' : ''} · ${new Date(result.captured_at).toLocaleTimeString()}`));
  const details = element('details', 'preview-details');
  details.append(element('summary', '', '查看正文'), element('div', 'preview-text', result.text));
  node.append(details);
  if (result.degraded) node.append(element('p', 'job-reason', '人工处理超时后的匿名回退，只代表部分公开正文，不是完整抓取。静态回退无法验证网站全部 CSS 可见性。'));
  const actions = element('div', 'actions');
  actions.append(button('导出 Markdown', 'export-md', job?.id || 'local'), button('导出 JSON', 'export-json', job?.id || 'local'));
  if (job && !TERMINAL.includes(job.state)) actions.append(button('取消任务', 'cancel', job.id));
  node.append(actions);
  return node;
}

function render() {
  const bridge = controller.bridge;
  const active = document.activeElement?.dataset;
  const focusKey = active?.action ? [active.action, active.id || ''] : null;
  $('connection-status').textContent = bridge.connected ? '已连接' : '未连接';
  $('connection-status').classList.toggle('connected', bridge.connected);
  $('disconnect-button').disabled = !bridge.connected;
  $('auto-enabled').checked = controller.autoEnabled;
  $('auto-enabled').disabled = !bridge.connected;
  $('revoke-all').disabled = Object.keys(controller.grants).length === 0;
  if (!$('relay-url').value && bridge.url) $('relay-url').value = bridge.url;
  const grants = $('grants-list'); grants.replaceChildren();
  for (const [origin, grant] of Object.entries(controller.grants)) {
    const row = element('div', 'grant-row');
    const minutes = Math.max(0, Math.ceil((grant.expiresAt - Date.now()) / 60000));
    row.append(element('span', '', `${origin} · ${minutes ? `${minutes} 分钟` : '已过期'}`));
    row.append(button('撤销', 'revoke', origin)); grants.append(row);
  }
  for (const id of ['jobs-list', 'human-list', 'preview-list']) $(id).replaceChildren();
  const jobs = Object.values(bridge.jobs).sort((a, b) => b.created_at.localeCompare(a.created_at));
  $('job-count').textContent = `${jobs.filter(job => !TERMINAL.includes(job.state)).length} 个进行中`;
  if (controller.localPreview) $('preview-list').append(previewCard(controller.localPreview));
  for (const job of jobs) {
    if (job.state === 'awaiting_human') $('human-list').append(card(job));
    else if (['awaiting_share', 'completed'].includes(job.state) && bridge.previews[job.id]) $('preview-list').append(previewCard(bridge.previews[job.id], job));
    else $('jobs-list').append(card(job));
  }
  for (const id of ['jobs-list', 'human-list', 'preview-list']) if (!$(id).childElementCount) $(id).append(emptyContent[id].cloneNode(true));
  if (focusKey) [...document.querySelectorAll('button[data-action]')].find(node => node.dataset.action === focusKey[0] && (node.dataset.id || '') === focusKey[1])?.focus({ preventScroll: true });
}

function guard(promise) { return Promise.resolve(promise).catch(error => notice(error.message || '操作失败，请重试', true)); }

$('connect-form').addEventListener('submit', event => {
  event.preventDefault();
  $('connect-button').disabled = true;
  // Invoke before awaiting anything so Chrome receives the local user gesture.
  void guard(controller.connect($('relay-url').value, $('browser-token').value)).finally(() => {
    $('browser-token').value = '';
    $('connect-button').disabled = false;
    if (controller.bridge.connected) document.querySelector('.connection').open = false;
  });
});
$('disconnect-button').addEventListener('click', () => void guard(controller.disconnect()));
$('auto-enabled').addEventListener('change', event => void guard(controller.toggleAutomatic(event.target.checked)));
$('revoke-all').addEventListener('click', () => void guard(controller.revoke()));
$('cleanup-tabs').addEventListener('click', () => void guard(controller.cleanupTabs()));
$('local-capture').addEventListener('click', () => void guard(controller.captureLocal()));
document.addEventListener('click', event => {
  const target = event.target.closest('button[data-action]');
  if (!target) return;
  const { action, id } = target.dataset;
  if (action === 'authorize') void guard(controller.authorize(id));
  else if (action === 'resume') void guard(controller.resume(id));
  else if (action === 'cancel') void guard(controller.cancel(id));
  else if (action === 'open') void guard(controller.openTab(id));
  else if (action === 'revoke') void guard(controller.revoke(id));
  else if (action.startsWith('export-')) {
    const result = id === 'local' ? controller.localPreview : controller.bridge.previews[id];
    if (!result) { notice('本地预览不存在或已过期', true); return; }
    const markdown = action === 'export-md';
    const content = markdown ? `${result.markdown}\n\nSource: ${result.url}\nCaptured: ${result.captured_at}\nQuality: ${result.quality}\n` : JSON.stringify(result, null, 2);
    const url = URL.createObjectURL(new Blob([content], { type: markdown ? 'text/markdown;charset=utf-8' : 'application/json;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = `armory-${id}.${markdown ? 'md' : 'json'}`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
});
window.addEventListener('pagehide', () => { if (controller.interval) clearInterval(controller.interval); });
if (globalThis.chrome?.storage?.local && globalThis.chrome?.runtime?.id) {
  void guard(controller.init());
} else {
  notice('界面预览：请在 Chrome 中加载扩展以使用采集功能。此页面未连接浏览器、不会采集数据。');
  $('connection-status').textContent = '界面预览';
  document.querySelectorAll('button,input').forEach(node => { node.disabled = true; });
}
