import { relayURL, validateJob, validateResult, boundedResult, persistentEntry, nextState, TERMINAL } from './core.mjs';

export class BridgeHTTPError extends Error {
  constructor(status) {
    super(`中继请求失败（HTTP ${status}）；未自动重试操作`);
    this.name = 'BridgeHTTPError';
    this.status = status;
    this.permanent = status >= 400 && status < 500 && ![408, 409, 425, 429].includes(status);
  }
}

export class ResultContractError extends Error {
  constructor() { super('本地采集结果不符合数据契约；停止该任务，未发送未经验证的正文'); this.name = 'ResultContractError'; this.permanent = true; }
}

export class Bridge {
  constructor(onChange = () => {}) {
    this.url = '';
    this.token = '';
    this.jobs = {};
    this.previews = {};
    this.onChange = onChange;
    this.generation = 0;
  }

  async init() {
    await chrome.storage.session.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' });
    const [local, session] = await Promise.all([chrome.storage.local.get(['relayURL', 'armoryRecords']), chrome.storage.session.get(['browserConnection', 'armoryPreviews'])]);
    this.url = local.relayURL || '';
    if (session.browserConnection?.url === this.url) this.token = session.browserConnection.token || '';
    this.jobs = local.armoryRecords?.[this.url] || {};
    this.previews = session.armoryPreviews?.[this.url] || {};
    // Re-validate persisted metadata; it never authorizes a different URL.
    this.jobs = Object.fromEntries(Object.entries(this.jobs).flatMap(([id, entry]) => {
      try { return id === entry.id ? [[id, persistentEntry(entry, entry)]] : []; } catch { return []; }
    }));
  }

  async mergeLocalLinkage() {
    const { armoryRecords = {} } = await chrome.storage.local.get('armoryRecords');
    for (const [id, entry] of Object.entries(armoryRecords[this.url] || {})) {
      if (!this.jobs[id]) continue;
      this.jobs[id] = persistentEntry(this.jobs[id], entry);
    }
  }

  get connected() { return Boolean(this.url && this.token); }

  async request(path, { method = 'GET', body } = {}) {
    if (!this.connected) throw new Error('请先连接中继');
    const generation = this.generation;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(`${this.url}${path}`, {
        method, headers: { Authorization: `Bearer ${this.token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', signal: controller.signal,
      });
      if (generation !== this.generation) throw new Error('连接已改变，本次操作已停止');
      if (!response.ok) throw new BridgeHTTPError(response.status);
      const text = await response.text();
      if (text.length > 2000000) throw new Error('中继响应超过限制');
      return JSON.parse(text);
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('中继请求超时。请检查连接后手动重试');
      throw error;
    } finally { clearTimeout(timer); }
  }

  async connect(url, token) {
    const normalized = relayURL(url);
    if (typeof token !== 'string' || token.length < 1 || token.length > 4096 || /[\r\n]/.test(token)) throw new Error('Browser token 无效');
    this.generation++;
    this.url = normalized;
    this.token = token;
    const [local, session] = await Promise.all([chrome.storage.local.get('armoryRecords'), chrome.storage.session.get('armoryPreviews')]);
    this.jobs = local.armoryRecords?.[normalized] || {};
    this.previews = session.armoryPreviews?.[normalized] || {};
    await chrome.storage.local.set({ relayURL: normalized });
    await chrome.storage.session.set({ browserConnection: { url: normalized, token } });
    try { await this.poll(); } catch (error) { await this.disconnect(); throw error; }
  }

  async disconnect() {
    this.generation++;
    this.token = '';
    await chrome.storage.session.remove('browserConnection');
    this.onChange();
  }

  async persist() {
    const { armoryRecords = {} } = await chrome.storage.local.get('armoryRecords');
    for (const [id, disk] of Object.entries(armoryRecords[this.url] || {})) {
      if (this.jobs[id] && (disk.localUpdatedAt || 0) > (this.jobs[id].localUpdatedAt || 0)) this.jobs[id] = persistentEntry(this.jobs[id], disk);
    }
    // Bound history to the latest 100 tasks, retaining nonterminal tasks first.
    const entries = Object.values(this.jobs).sort((a, b) => Number(TERMINAL.includes(a.state) || a.localTerminal) - Number(TERMINAL.includes(b.state) || b.localTerminal) || b.updated_at.localeCompare(a.updated_at)).slice(0, 100);
    this.jobs = Object.fromEntries(entries.map(entry => [entry.id, persistentEntry(entry, entry)]));
    armoryRecords[this.url] = this.jobs;
    await chrome.storage.local.set({ armoryRecords });
    this.onChange();
  }

  accept(raw) {
    const previous = this.jobs[raw?.id];
    const job = validateJob(raw, previous);
    this.jobs[job.id] = persistentEntry(job, previous);
    return this.jobs[job.id];
  }

  async poll() {
    await this.mergeLocalLinkage();
    let response;
    try { response = await this.request('/v1/browser/jobs'); }
    catch (error) { if ([401, 403].includes(error.status)) await this.disconnect(); throw error; }
    if (!Array.isArray(response.jobs) || response.jobs.length > 100) throw new Error('中继任务列表格式无效');
    const seen = new Set();
    for (const raw of response.jobs) { const job = this.accept(raw); seen.add(job.id); }
    // The queue excludes terminal tasks; explicitly reconcile disappeared jobs.
    for (const job of Object.values(this.jobs)) if (!TERMINAL.includes(job.state) && !job.localTerminal && !seen.has(job.id)) {
      try {
        const status = await this.request(`/v1/jobs/${encodeURIComponent(job.id)}`);
        this.accept(status.job);
      } catch (error) {
        if ([404, 410].includes(error.status)) {
          // A lazy-TTL deletion concerns this task only. Do not let stale local
          // metadata prevent freshly queued jobs from running on every poll.
          this.jobs[job.id] = persistentEntry(job, { ...job, localTerminal: true, localUpdatedAt: Date.now(), localError: `本地任务已终止：中继任务已到期或删除（HTTP ${error.status}）。` });
          continue;
        }
        if ([401, 403].includes(error.status)) await this.disconnect();
        throw error;
      }
    }
    await this.persist();
  }

  async refresh(id) {
    await this.mergeLocalLinkage();
    const response = await this.request(`/v1/jobs/${encodeURIComponent(id)}`);
    const job = this.accept(response.job);
    await this.persist();
    return job;
  }

  async event(id, type, details = {}) {
    const job = this.jobs[id];
    if (!job) throw new Error('任务不存在');
    nextState(job.state, type);
    const body = { type };
    if (typeof details.reason === 'string') body.reason = details.reason.slice(0, 1000);
    if (type === 'complete') {
      try { body.result = validateResult(details.result, job); } catch { throw new ResultContractError(); }
    }
    // No result payload is ever sent by approve, resume, preview_ready or polling.
    const response = await this.request(`/v1/browser/jobs/${encodeURIComponent(id)}/events`, { method: 'POST', body });
    this.accept(response.job);
    await this.persist();
    return this.jobs[id];
  }

  async setLocal(id, properties) {
    this.jobs[id] = persistentEntry(this.jobs[id], { ...this.jobs[id], ...properties, localUpdatedAt: Date.now() });
    await this.persist();
  }

  async setPreview(id, result) {
    let safe;
    try { safe = boundedResult(result, this.jobs[id]); } catch { throw new ResultContractError(); }
    this.previews[id] = safe;
    this.previews = Object.fromEntries(Object.entries(this.previews).sort((a, b) => b[1].captured_at.localeCompare(a[1].captured_at)).slice(0, 12));
    // Keep only this relay's last 12 previews; never let historic relay bodies fill session storage.
    await chrome.storage.session.set({ armoryPreviews: { [this.url]: this.previews } });
    this.onChange();
  }
}
