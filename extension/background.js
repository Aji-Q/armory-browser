// No polling, page reading, remote execution, or automatic tabs in the worker.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});
chrome.runtime.onStartup.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});
chrome.tabs.onRemoved.addListener(async tabId => {
  // Preserve the job identity but remove stale tab linkage after a user closes it.
  const { armoryRecords = {} } = await chrome.storage.local.get('armoryRecords');
  let changed = false;
  for (const jobs of Object.values(armoryRecords)) for (const entry of Object.values(jobs)) {
    if (entry.tabId === tabId) {
      entry.tabId = null;
      entry.tabClosed = true;
      entry.localUpdatedAt = Date.now();
      entry.localError = '专属标签页已关闭。请取消此任务并由 Agent 重新创建，不会自动新建标签页。';
      changed = true;
    }
  }
  if (changed) await chrome.storage.local.set({ armoryRecords });
});

// Serialize ownership across multiple open side panels without a persistent worker.
let leaseQueue = Promise.resolve();
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || !sender.url?.startsWith(chrome.runtime.getURL(''))) return;
  if (!['armory-lock', 'armory-unlock'].includes(message?.type) || typeof message.key !== 'string' || typeof message.owner !== 'string') return;
  // Session persistence survives normal MV3 worker suspension. This queue makes
  // the read-modify-write atomic among concurrent panels in the active worker.
  leaseQueue = leaseQueue.then(async () => {
    const { armoryLeases = {} } = await chrome.storage.session.get('armoryLeases');
    for (const [key, lease] of Object.entries(armoryLeases)) if (lease.until < Date.now()) delete armoryLeases[key];
    if (message.type === 'armory-lock') {
      const lease = armoryLeases[message.key];
      const allowed = !lease || lease.owner === message.owner;
      if (allowed) armoryLeases[message.key] = { owner: message.owner, until: Date.now() + 120000 };
      await chrome.storage.session.set({ armoryLeases });
      respond({ allowed });
    } else {
      if (armoryLeases[message.key]?.owner === message.owner) delete armoryLeases[message.key];
      await chrome.storage.session.set({ armoryLeases });
      respond({ released: true });
    }
  }).catch(() => respond({ allowed: false }));
  return true;
});
