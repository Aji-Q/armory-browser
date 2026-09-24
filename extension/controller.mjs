import { Bridge, ResultContractError } from './bridge.mjs';
import { webURL, relayURL, permissionPattern, makeGrant, hasGrant, humanDeadline, nextAutomaticAction, canCapture, sameOrigin, sameResource, validateResult, TERMINAL } from './core.mjs';
import { extractVisiblePage } from './extract.mjs';
import { anonymousFallback } from './fallback.mjs';

export const CAPTURE_WAIT_BUDGET_MS = 8000;
export const CAPTURE_SAMPLE_INTERVAL_MS = 800;
const CAPTURE_MAX_ATTEMPTS = 11;

export class Controller {
  constructor({ onChange = () => {}, onNotice = () => {} } = {}) {
    this.onChange = onChange;
    this.onNotice = onNotice;
    this.bridge = new Bridge(onChange);
    this.grants = {};
    this.autoEnabled = false;
    this.busy = new Set();
    this.retryAfter = new Map();
    this.polling = false;
    this.localPreview = null;
    this.interval = null;
    this.owner = crypto.randomUUID();
  }

  async init() {
    await this.bridge.init();
    await this.loadGrants();
    const { localPreview } = await chrome.storage.session.get('localPreview');
    this.localPreview = localPreview || null;
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== 'session') return;
      if (changes.armoryConsent) {
        const consent = changes.armoryConsent.newValue?.[this.bridge.url];
        this.grants = consent?.grants || {};
        this.autoEnabled = consent?.autoEnabled === true;
      }
      if (changes.browserConnection) {
        const connection = changes.browserConnection.newValue;
        const token = connection?.url === this.bridge.url ? connection.token : '';
        if (token !== this.bridge.token) { this.bridge.generation++; this.bridge.token = token || ''; }
      }
      this.onChange();
    });
    this.onChange();
    this.interval = setInterval(() => void this.tick(), 2000);
    await this.tick();
  }

  async loadGrants() {
    const { armoryConsent = {} } = await chrome.storage.session.get('armoryConsent');
    const consent = armoryConsent[this.bridge.url];
    this.grants = consent?.grants || {};
    this.autoEnabled = consent?.autoEnabled === true;
  }

  async saveGrants() {
    const { armoryConsent = {} } = await chrome.storage.session.get('armoryConsent');
    armoryConsent[this.bridge.url] = { grants: this.grants, autoEnabled: this.autoEnabled };
    await chrome.storage.session.set({ armoryConsent });
    this.onChange();
  }

  async connect(url, token) {
    const normalized = relayURL(url);
    // Called directly from the local submit gesture, before any asynchronous work.
    const allowed = await chrome.permissions.request({ origins: [permissionPattern(normalized)] });
    if (!allowed) throw new Error('中继访问权限被拒绝，尚未连接');
    await this.bridge.connect(normalized, token);
    await this.loadGrants();
    this.onNotice('已连接。已授权站点自动采集与回传；新站点会等待一次会话授权。');
    await this.tick();
  }

  async disconnect() {
    this.autoEnabled = false;
    this.grants = {};
    await this.saveGrants();
    await this.bridge.disconnect();
    this.onNotice('已断开并撤销本会话自动授权。已发送的数据无法撤回；专属标签页保留。');
  }

  async authorize(id) {
    const job = this.bridge.jobs[id];
    if (!job || !this.bridge.connected || TERMINAL.includes(job.state)) throw new Error('任务当前不可授权');
    const scope = this.bridge.url;
    const grant = makeGrant(scope, job.url);
    // Host permission prompt occurs only in this explicit origin-consent gesture.
    const allowed = await chrome.permissions.request({ origins: [permissionPattern(job.url)] });
    if (!allowed) {
      await this.bridge.setLocal(id, { localError: '网站权限被拒绝；任务保持等待，不会打开或读取页面。' });
      throw new Error('网站权限被拒绝');
    }
    if (scope !== this.bridge.url || !this.bridge.connected) throw new Error('中继已改变，请重新授权');
    this.grants[grant.origin] = grant;
    this.autoEnabled = true;
    await this.saveGrants();
    await this.bridge.setLocal(id, { localError: '' });
    this.onNotice(`已授权 ${grant.origin}：8 小时内自动采集并回传到 ${scope}。可随时暂停或撤销。`);
    await this.tick();
  }

  async toggleAutomatic(enabled) {
    this.autoEnabled = Boolean(enabled);
    await this.saveGrants();
    this.onNotice(enabled ? '已恢复已授权站点的自动处理。' : '自动处理已暂停。不会新开页面、读取正文或继续回传。已发出的请求可能已被中继接收。');
    if (enabled) await this.tick();
  }

  async revoke(origin) {
    if (origin) delete this.grants[origin];
    else { this.grants = {}; this.autoEnabled = false; }
    await this.saveGrants();
    this.onNotice('会话授权已撤销，不再自动采集或回传。Chrome 已授予的网站权限可在扩展设置中另外移除。');
  }

  allowed(job) { return this.bridge.connected && this.autoEnabled && hasGrant(this.grants, this.bridge.url, job.url); }
  async assertAllowed(job, generation) {
    if (generation !== this.bridge.generation || !this.allowed(job)) throw new Error('本会话授权已暂停、过期、撤销或连接已改变；操作停止');
    // onChanged improves responsiveness but is not an authorization boundary:
    // another panel can revoke while its event is still queued. Read the shared
    // session immediately before each sensitive action and again before upload.
    const { armoryConsent = {}, browserConnection } = await chrome.storage.session.get(['armoryConsent', 'browserConnection']);
    const consent = armoryConsent[this.bridge.url];
    const storedGrant = consent?.autoEnabled === true && hasGrant(consent.grants, this.bridge.url, job.url);
    const storedConnection = browserConnection?.url === this.bridge.url && browserConnection?.token === this.bridge.token && Boolean(this.bridge.token);
    if (generation !== this.bridge.generation || !this.allowed(job) || !storedGrant || !storedConnection) throw new Error('共享会话授权或连接已撤销、暂停、过期或改变；操作停止');
  }

  async tick() {
    if (this.polling || !this.bridge.connected) { this.onChange(); return; }
    this.polling = true;
    try {
      await this.bridge.poll();
      for (const job of Object.values(this.bridge.jobs)) {
        if (this.busy.size >= 3) break;
        if (this.busy.has(job.id) || (this.retryAfter.get(job.id) || 0) > Date.now()) continue;
        const action = nextAutomaticAction(job, this.grants, this.bridge.url, this.autoEnabled);
        if (action) void this.process(job.id, action);
      }
    } catch (error) { this.onNotice(error.message, true); }
    finally { this.polling = false; this.onChange(); }
  }

  async process(id, action) {
    if (this.busy.has(id)) return;
    this.busy.add(id);
    this.onChange();
    const generation = this.bridge.generation;
    const lockKey = `${this.bridge.url}|${id}`;
    try {
      const lease = await chrome.runtime.sendMessage({ type: 'armory-lock', key: lockKey, owner: this.owner });
      if (!lease?.allowed) return;
      let job = await this.bridge.refresh(id);
      await this.assertAllowed(job, generation);
      action = nextAutomaticAction(job, this.grants, this.bridge.url, this.autoEnabled);
      if (!action) return;
      if (action === 'approve_capture') {
        await this.bridge.event(id, 'approve');
        await this.assertAllowed(job, generation);
        const tab = await chrome.tabs.create({ url: job.url, active: false });
        await this.bridge.setLocal(id, { tabId: tab.id, approved: true, ownedTab: true, tabClosed: false, localError: '' });
        job = this.bridge.jobs[id];
        await this.waitForTab(tab.id, id, generation);
        await this.capture(id, generation);
      } else if (action === 'capture') {
        if (!job.tabId || !job.approved) {
          if (job.tabClosed) { await this.bridge.event(id, 'fail', { reason: '用户已关闭专属标签页；不会自动重新打开。其他任务继续。' }); return; }
          // A crash after approve may leave no tab. Session consent permits one replacement.
          await this.assertAllowed(job, generation);
          const tab = await chrome.tabs.create({ url: job.url, active: false });
          await this.bridge.setLocal(id, { tabId: tab.id, approved: true, ownedTab: true, tabClosed: false, localError: '' });
          await this.waitForTab(tab.id, id, generation);
        }
        await this.capture(id, generation);
      } else if (action === 'timeout_fallback') {
        await this.bridge.event(id, 'timeout', { reason: '人工等待期限已到，自动回退匿名公开请求；其他任务继续。' });
        await this.bridge.setLocal(id, { timeoutFallback: true });
        await this.fallback(id, generation);
      } else if (action === 'anonymous_fallback') await this.fallback(id, generation);
      else if (action === 'share') await this.share(id, generation);
    } catch (error) {
      if (generation === this.bridge.generation && this.bridge.jobs[id]) {
        if (error.permanent) await this.stopPermanent(id, error, generation);
        else {
          await this.bridge.setLocal(id, { localError: error.message });
          this.retryAfter.set(id, Date.now() + 15000);
        }
      }
      this.onNotice(error.message, true);
    } finally {
      await chrome.runtime.sendMessage({ type: 'armory-unlock', key: lockKey, owner: this.owner }).catch(() => {});
      this.busy.delete(id); this.onChange();
    }
  }

  async stopPermanent(id, error, generation) {
    const kind = error.status ? `永久请求错误（HTTP ${error.status}）` : '本地结果不符合数据契约';
    const reason = `${kind}：本地任务已终止，不再自动重试；修正配置后请创建新任务。`;
    this.retryAfter.delete(id);
    await this.bridge.setLocal(id, { localTerminal: true, localError: reason });
    if ([401, 403].includes(error.status)) {
      // Invalid credentials cannot report a server transition. Stop locally and
      // disconnect explicitly rather than claiming the remote task became failed.
      await this.bridge.disconnect();
      return;
    }
    if ([404, 410].includes(error.status)) return; // The remote task no longer exists.
    try {
      const latest = await this.bridge.refresh(id);
      await this.assertAllowed(latest, generation);
      if (!TERMINAL.includes(latest.state)) await this.bridge.event(id, 'fail', { reason });
    } catch {
      // Preserve the honest local terminal marker if even failure reporting fails.
      await this.bridge.setLocal(id, { localTerminal: true, localError: reason });
    }
  }

  async waitForTab(tabId, id, generation) {
    const started = Date.now();
    for (let attempt = 0; attempt < 24 && Date.now() - started < CAPTURE_WAIT_BUDGET_MS; attempt++) {
      if (id) await this.assertAllowed(this.bridge.jobs[id], generation);
      const tab = await chrome.tabs.get(tabId);
      if (tab.status === 'complete') return;
      await new Promise(resolve => setTimeout(resolve, 350));
    }
    // Navigation has its own hard budget. Readiness is based on actual visible
    // extraction samples below, never document.complete + an arbitrary 700 ms.
  }

  async capture(id, generation) {
    const started = Date.now();
    let previous = '', lastReason = '当前页面没有足够的稳定可见正文，请手动处理。';
    for (let attempt = 0; attempt < CAPTURE_MAX_ATTEMPTS && Date.now() - started <= CAPTURE_WAIT_BUDGET_MS; attempt++) {
      const job = await this.bridge.refresh(id);
      await this.assertAllowed(job, generation);
      if (job.state !== 'running') return;
      const tab = await chrome.tabs.get(job.tabId);
      const check = canCapture(job, tab, job);
      if (!check.allowed) { await this.requireHuman(id, check.reason); return; }
      const permitted = await chrome.permissions.contains({ origins: [permissionPattern(job.url)] });
      if (!permitted) throw new Error('网站权限已撤销，请重新授权此站点');
      await this.assertAllowed(job, generation);
      const outputs = await chrome.scripting.executeScript({ target: { tabId: job.tabId, frameIds: [0] }, func: extractVisiblePage, args: [webURL(job.url).origin, job.max_chars], world: 'ISOLATED' });
      await this.assertAllowed(job, generation);
      // A navigation during script execution must not turn another page into the
      // requested article. Check the live tab and the captured URL independently.
      const afterTab = await chrome.tabs.get(job.tabId);
      const afterCheck = canCapture(job, afterTab, job);
      if (!afterCheck.allowed) { await this.requireHuman(id, afterCheck.reason); return; }
      const output = outputs[0]?.result;
      if (output?.result && !output.humanRequired) {
        let safe;
        try { safe = validateResult(output.result, job); } catch { throw new ResultContractError(); }
        const signature = JSON.stringify([safe.url, safe.title, safe.text, safe.markdown, safe.links]);
        if (signature === previous) {
          const latest = await this.bridge.refresh(id);
          await this.assertAllowed(latest, generation);
          if (latest.state !== 'running') return;
          await this.bridge.setPreview(id, safe);
          await this.assertAllowed(latest, generation);
          await this.bridge.event(id, 'preview_ready');
          await this.share(id, generation);
          return;
        }
        previous = signature;
        lastReason = '正文仍在动态变化，有限等待结束后仍未稳定；请稍后手动恢复。';
      } else {
        previous = '';
        lastReason = output?.reason || '页面仍在加载或没有足够可见正文。';
        const explicitWall = ['identity_page', 'origin_changed', 'login_wall', 'access_challenge'].includes(output?.reasonCode);
        const legacyWall = !output?.reasonCode && /登录|验证码|访问验证|拒绝访问|付费墙|身份认证|login|sign.?in|captcha|access denied/i.test(lastReason);
        if (explicitWall || legacyWall) { await this.requireHuman(id, lastReason); return; }
      }
      if (attempt + 1 >= CAPTURE_MAX_ATTEMPTS || Date.now() - started + CAPTURE_SAMPLE_INTERVAL_MS > CAPTURE_WAIT_BUDGET_MS) break;
      // Yield; other queued jobs keep running while this task waits for readiness.
      await new Promise(resolve => setTimeout(resolve, CAPTURE_SAMPLE_INTERVAL_MS));
    }
    const latest = await this.bridge.refresh(id);
    await this.assertAllowed(latest, generation);
    if (latest.state === 'running') await this.requireHuman(id, `自动等待已达到 8 秒上限：${lastReason}`);
  }

  async requireHuman(id, reason) {
    const job = this.bridge.jobs[id];
    if (job.state !== 'running') return;
    await this.assertAllowed(job, this.bridge.generation);
    await this.bridge.event(id, 'human_required', { reason });
    await this.bridge.setLocal(id, { humanDeadline: humanDeadline(this.bridge.jobs[id]), hadHuman: true, localError: '' });
    this.onNotice('有任务需要人工处理，倒计时结束后自动匿名回退；其他任务继续。');
  }

  async fallback(id, generation) {
    const job = this.bridge.jobs[id];
    await this.assertAllowed(job, generation);
    try {
      const result = await anonymousFallback(job);
      await this.assertAllowed(job, generation);
      const latest = await this.bridge.refresh(id);
      if (latest.state !== 'running') return;
      await this.bridge.setPreview(id, result);
      await this.bridge.event(id, 'preview_ready', { reason: '人工等待超时：匿名回退取得部分正文，非完整结果。' });
      await this.share(id, generation);
    } catch (error) {
      await this.assertAllowed(job, generation);
      const latest = await this.bridge.refresh(id);
      if (!TERMINAL.includes(latest.state)) await this.bridge.event(id, 'fail', { reason: `匿名回退失败：${error.message}` });
    }
  }

  async share(id, generation) {
    const job = await this.bridge.refresh(id);
    await this.assertAllowed(job, generation);
    if (job.state !== 'awaiting_share') return;
    const result = this.bridge.previews[id];
    if (!result) {
      await this.bridge.event(id, 'fail', { reason: '本地会话预览已丢失，无法返回已验证结果。请重新创建任务。' });
      return;
    }
    // This is authorized by the explicit per-origin, per-relay, expiring auto-share grant.
    await this.bridge.event(id, 'complete', { result });
    await this.cleanupTab(id);
  }

  async resume(id) {
    if (this.busy.has(id)) return;
    const generation = this.bridge.generation;
    const job = await this.bridge.refresh(id);
    await this.assertAllowed(job, generation);
    if (Date.now() >= humanDeadline(job)) { await this.process(id, 'timeout_fallback'); return; }
    let tab = await chrome.tabs.get(job.tabId);
    if (!sameResource(tab.url, job.url) && sameOrigin(tab.url, job.url) && job.ownedTab && job.approved && tab.id === job.tabId) {
      // This navigation is the user's explicit Resume action on our own tab. It
      // targets only the original task URL; never inspect or interact with IdP.
      await this.assertAllowed(job, generation);
      tab = await chrome.tabs.update(job.tabId, { url: job.url });
      await this.waitForTab(tab.id, id, generation);
      tab = await chrome.tabs.get(job.tabId);
    }
    const check = canCapture(job, tab, job);
    if (!check.allowed) throw new Error(check.reason);
    await this.assertAllowed(job, generation);
    await this.bridge.event(id, 'resume');
    await this.bridge.setLocal(id, { humanDeadline: null, localError: '' });
    await this.process(id, 'capture');
  }

  async cancel(id) {
    await this.bridge.refresh(id);
    await this.bridge.event(id, 'cancel', { reason: '用户在浏览器侧栏取消任务。' });
    await this.cleanupTab(id);
  }

  async cleanupTab(id) {
    const job = this.bridge.jobs[id];
    if (!job?.ownedTab || !job.tabId || job.hadHuman || !TERMINAL.includes(job.state)) return;
    try {
      const tab = await chrome.tabs.get(job.tabId);
      if (!tab.active) {
        await chrome.tabs.remove(tab.id);
        await this.bridge.setLocal(id, { tabId: null, tabClosed: true });
      }
    } catch { /* Already closed. Never close unrelated or manually used tabs. */ }
  }

  async cleanupTabs() {
    for (const job of Object.values(this.bridge.jobs)) await this.cleanupTab(job.id);
    this.onNotice('已清理工具创建且无需人工协作的已结束后台页。当前活动页和人工协作页保留。');
  }

  async openTab(id) {
    const job = this.bridge.jobs[id];
    if (!job?.tabId) throw new Error('此任务没有专属标签页');
    const tab = await chrome.tabs.update(job.tabId, { active: true });
    if (tab.windowId !== undefined) await chrome.windows.update(tab.windowId, { focused: true });
  }

  async captureLocal() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !tab.url) throw new Error('请先激活网页并点击扩展图标，再采集当前页面');
    const url = webURL(tab.url);
    const outputs = await chrome.scripting.executeScript({ target: { tabId: tab.id, frameIds: [0] }, func: extractVisiblePage, args: [url.origin, 20000], world: 'ISOLATED' });
    const output = outputs[0]?.result;
    if (!output?.result) throw new Error(output?.reason || '此页没有可提取正文');
    this.localPreview = validateResult(output.result, { url: tab.url, max_chars: 20000 });
    await chrome.storage.session.set({ localPreview: this.localPreview });
    this.onChange();
    this.onNotice('当前页面已提取，仅本地预览与导出，不上传中继。');
  }
}
