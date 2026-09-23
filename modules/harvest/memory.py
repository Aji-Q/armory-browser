"""按域名记住后端的历史表现 —— 别每次都把同样的坑重踩一遍。

记的是**失败**而不是成功。理由是不对称成本：轻档（静态）一跳几百毫秒，
重档（浏览器）一跳 5–10 秒，差 20 倍。所以静态永远跑，给它做记忆省不出东西；
真正的浪费在于每次都去重试那些上次就失败过的重档 —— 每一次重试都是一个完整的
浏览器启动。

TTL 分两档：一般失败 24 小时，403/429/503 这类 1 小时。后者更可能是 IP 的瞬时
状态而不是站点结构变化，过一小时就该重新试。

排的是**顺序**，不是删除：冷却中的后端排到最后仍然保留，因为站点会变，一个被
记忆判死的后端如果永远不试，就再也发现不了它已经恢复。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

ARMORY_HOME = Path.home() / ".armory"
DEFAULT_STORE = ARMORY_HOME / "backend_memory.json"

DEFAULT_TTL = 24 * 3600
TRANSIENT_TTL = 3600
TRANSIENT_STATUS = (403, 429, 503)


class BackendMemory:
    """域名 → 各后端的成功/失败痕迹。文件存储，单机自用够用。"""

    def __init__(self, path=None, enabled=True):
        self.path = Path(path) if path else DEFAULT_STORE
        self.enabled = enabled
        self._data = None
        # 并发抓取时多个线程会同时 record —— 写同一个临时文件会互相踩,
        # 所以写盘串行化,临时名也带上 pid 区分进程。
        self._lock = threading.Lock()
        self._warned = False

    # ------------------------------------------------------------ 存储

    def _load(self):
        if self._data is None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = loaded if isinstance(loaded, dict) else {}
            except Exception:
                self._data = {}
        return self._data

    def _flush(self):
        if not self.enabled or self._data is None:
            return
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                os.chmod(tmp, 0o600)      # 这里没有凭证,但域名清单本身也不必外露
                tmp.replace(self.path)
            except Exception as exc:
                # 静默吞掉的话,记忆"看起来在生效"其实一条没写。提示一次就够,
                # 不要每次抓取都刷屏。
                if not self._warned:
                    self._warned = True
                    print(f"[!] 后端记忆写入失败(后续不再提示): {exc}", file=sys.stderr)

    @staticmethod
    def host_of(url):
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()

    # ------------------------------------------------------------ 读写

    def _record(self, url):
        host = self.host_of(url)
        if not host:
            return None
        return self._load().setdefault(host, {})

    def is_cooling(self, url, backend):
        """该后端在这个域名上是不是刚失败过，还在冷却期内。"""
        if not self.enabled:
            return False
        rec = self._load().get(self.host_of(url)) or {}
        failed = (rec.get("failed") or {}).get(backend)
        if not failed:
            return False
        ttl = TRANSIENT_TTL if failed.get("status") in TRANSIENT_STATUS else DEFAULT_TTL
        return (time.time() - float(failed.get("ts") or 0)) < ttl

    def rank(self, url, candidates):
        """按历史重排候选：上次成功的排最前，冷却中的排到最后。

        candidates 是 [(name, fn), ...]。只调顺序，不删 —— 冷却中的仍然保留在
        末尾，站点恢复时还能被发现。
        """
        if not self.enabled or len(candidates) < 2:
            return candidates
        rec = self._load().get(self.host_of(url)) or {}
        ok_backend = rec.get("ok_backend")

        def key(item):
            name = item[0]
            if name == ok_backend:
                return 0
            if self.is_cooling(url, name):
                return 2
            return 1

        return sorted(candidates, key=key)

    def record_ok(self, url, backend):
        if not self.enabled:
            return
        rec = self._record(url)
        if rec is None:
            return
        rec["ok_backend"] = backend
        rec["ok_ts"] = time.time()
        (rec.get("failed") or {}).pop(backend, None)
        self._flush()

    def record_fail(self, url, backend, status=None):
        if not self.enabled:
            return
        rec = self._record(url)
        if rec is None:
            return
        rec.setdefault("failed", {})[backend] = {"ts": time.time(), "status": status}
        if rec.get("ok_backend") == backend:
            # 上次成功的这次反而失败,说明站点变了 —— 别再用它当首选
            rec.pop("ok_backend", None)
        self._flush()

    def describe(self, url):
        """给人看的该域名历史。"""
        rec = self._load().get(self.host_of(url)) or {}
        if not rec:
            return "无记录"
        bits = []
        if rec.get("ok_backend"):
            age_h = (time.time() - float(rec.get("ok_ts") or 0)) / 3600
            bits.append(f"首选 {rec['ok_backend']}({age_h:.1f} 小时前成功)")
        for name, info in (rec.get("failed") or {}).items():
            cooling = "冷却中" if self.is_cooling(url, name) else "已过期"
            bits.append(f"{name} 失败于 {info.get('status')}({cooling})")
        return " | ".join(bits) or "无记录"
