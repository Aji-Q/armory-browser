#!/usr/bin/env python3
"""harvest 代理池 —— 健康检测、延迟评分、失败剔除、按站点避让

单 IP 上堆再多技巧也有天花板;要往上走,得靠池子。

池子不是「一个列表轮流取」就够了,真正决定成败的是三件事:

  1. **可用的才发**。代理会死、会变慢、会被目标站点单独封 —— 不检测就发,等于把
     失败率直接叠进抓取成功率里。
  2. **按站点记账**。同一个代理在 A 站被封,不影响它在 B 站干活。全局剔除太粗暴。
  3. **自己别把自己暴露**。每次请求换一个出口 IP,对风控来说就是「同一账号在多个
     城市之间瞬移」—— 比固定 IP 更可疑。所以绑定会话(同一站点尽量用同一个出口)。

评分用延迟 EMA + 连续失败计数。连续失败到阈值就冷却,冷却期内只降权不彻底禁用 ——
代理可能是临时抖动,永久拉黑会让你在长任务里越跑越少。
"""

from __future__ import annotations

import concurrent.futures
import random
import threading
import time
from dataclasses import dataclass, field


DEFAULT_CHECK_URL = "https://api.ipify.org?format=json"


@dataclass
class ProxyStat:
    url: str
    ok: int = 0
    fail: int = 0
    consecutive_fails: int = 0
    latency: float = 0.0          # 指数移动平均(秒)
    last_used: float = 0.0
    cooldown_until: float = 0.0
    total_bytes: int = 0
    # 分站点记账:某个站把某代理封了,不该牵连其他站
    site_fails: dict = field(default_factory=dict)
    site_cooldown: dict = field(default_factory=dict)

    @property
    def success_rate(self):
        total = self.ok + self.fail
        return self.ok / total if total else 0.0

    def available(self, site=None, now=None):
        now = now or time.monotonic()
        if self.cooldown_until > now:
            return False
        if site and self.site_cooldown.get(site, 0) > now:
            return False
        return True


class ProxyPool:
    """代理池。

    参数:
        proxies      代理 URL 列表
        check_url    健康检测目标(默认取出口 IP 的公共服务)
        cooldown     全局冷却秒数(连续失败达阈值后)
        site_cooldown 单站点冷却秒数
        fail_threshold 连续失败多少次进冷却
    """

    def __init__(self, proxies, check_url=DEFAULT_CHECK_URL,
                 timeout=8, workers=16, cooldown=120, site_cooldown=300,
                 fail_threshold=3, sticky=True):
        self.proxies = list(dict.fromkeys(proxies or []))
        self.check_url = check_url
        self.timeout = timeout
        self.workers = workers
        self.cooldown = cooldown
        self.site_cooldown = site_cooldown
        self.fail_threshold = fail_threshold
        self.sticky = sticky               # 同站点优先复用上次成功的代理
        self.stats = {url: ProxyStat(url) for url in self.proxies}
        self._lock = threading.Lock()
        self._site_binding = {}            # site -> proxy url
        self._rr = 0

    def __len__(self):
        return len(self.proxies)

    # ---------------------------------------------------------- 健康检测

    @staticmethod
    def _probe(proxy, check_url, timeout):
        import json
        import time as _t
        started = _t.monotonic()
        try:
            from curl_cffi import requests as cr
            resp = cr.get(check_url, proxy=proxy, timeout=timeout, impersonate="chrome")
            elapsed = _t.monotonic() - started
            if resp.status_code != 200:
                return proxy, False, elapsed, f"HTTP {resp.status_code}"
            try:
                body = json.loads(resp.text)
                ip = body.get("ip") or body.get("origin", "")[:40]
            except Exception:
                ip = resp.text.strip()[:40]
            return proxy, True, elapsed, ip
        except Exception as exc:
            return proxy, False, _t.monotonic() - started, f"{type(exc).__name__}"

    def check_all(self, verbose=True):
        """并发检测全部代理。返回可用的数量。"""
        if not self.proxies:
            return 0
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._probe, p, self.check_url, self.timeout)
                       for p in self.proxies]
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

        alive = 0
        for proxy, ok, latency, detail in results:
            stat = self.stats[proxy]
            if ok:
                alive += 1
                stat.ok += 1
                stat.consecutive_fails = 0
                stat.latency = latency if stat.latency == 0 else stat.latency * 0.7 + latency * 0.3
            else:
                stat.fail += 1
                stat.consecutive_fails += 1
                if stat.consecutive_fails >= self.fail_threshold:
                    stat.cooldown_until = time.monotonic() + self.cooldown
            if verbose:
                mark = "✓" if ok else "✗"
                lat = f"{latency * 1000:6.0f}ms" if ok else "      -"
                print(f"  {mark} {proxy:<40} {lat}  {detail}")
        return alive

    # ---------------------------------------------------------- 选路

    def pick(self, site=None):
        """挑一个代理。全都在冷却里时返回 None(调用方应直连或等待)。"""
        if not self.proxies:
            return None
        now = time.monotonic()

        if self.sticky and site:
            bound = self._site_binding.get(site)
            if bound and self.stats[bound].available(site, now):
                return bound

        with self._lock:
            candidates = [p for p in self.proxies if self.stats[p].available(site, now)]
            if not candidates:
                # 全在冷却:退而求其次,挑冷却最早结束的
                candidates = sorted(self.proxies, key=lambda p: self.stats[p].cooldown_until)[:1]
                if not candidates:
                    return None

            # 加权:成功率高的权重大,延迟低的小幅加权
            def weight(p):
                stat = self.stats[p]
                base = max(stat.success_rate, 0.05)
                if stat.latency:
                    base /= (0.2 + stat.latency)      # 200ms 上下浮动
                return base

            total = sum(weight(p) for p in candidates)
            if total <= 0:
                chosen = candidates[0]
            else:
                r = random.uniform(0, total)
                upto = 0.0
                chosen = candidates[-1]
                for p in candidates:
                    upto += weight(p)
                    if upto >= r:
                        chosen = p
                        break

        if self.sticky and site:
            self._site_binding[site] = chosen
        return chosen

    # ---------------------------------------------------------- 反馈

    def report(self, proxy, ok, latency=None, site=None, banned=False):
        """把请求结果写回池子。

        ok 指**代理是否连通**(拿到 HTTP 响应就算通);banned 指**目标是否拒绝了这个出口**
        (403/429/验证码)。两者独立 —— 代理通了但被目标封,记的是站点维度的账,
        不该把整个代理拉黑。
        """
        if not proxy or proxy not in self.stats:
            return
        stat = self.stats[proxy]
        stat.last_used = time.monotonic()

        if ok:
            stat.ok += 1
            stat.consecutive_fails = 0
            if latency:
                stat.latency = (latency if stat.latency == 0
                                else stat.latency * 0.7 + latency * 0.3)
            if site and not banned:
                stat.site_fails.pop(site, None)
        else:
            stat.fail += 1
            stat.consecutive_fails += 1
            if stat.consecutive_fails >= self.fail_threshold:
                stat.cooldown_until = time.monotonic() + self.cooldown

        if banned and site:
            stat.site_fails[site] = stat.site_fails.get(site, 0) + 1
            stat.site_cooldown[site] = time.monotonic() + self.site_cooldown
            # 该站在这个出口上被封,换一个
            if self._site_binding.get(site) == proxy:
                self._site_binding.pop(site, None)

    # ---------------------------------------------------------- 报表

    def stats_table(self):
        rows = []
        now = time.monotonic()
        for url, s in sorted(self.stats.items(), key=lambda kv: -kv[1].success_rate):
            if s.cooldown_until > now:
                state = f"冷却 {s.cooldown_until - now:.0f}s"
            elif s.ok == 0 and s.fail == 0:
                state = "未检测"
            elif s.success_rate == 0:
                state = "劣质"          # 失败过但未达阈值,别报成「可用」误导人
            elif s.success_rate < 0.5:
                state = "不稳定"
            else:
                state = "可用"
            rows.append({
                "proxy": url, "state": state, "ok": s.ok, "fail": s.fail,
                "success_rate": round(s.success_rate, 3),
                "latency_ms": round(s.latency * 1000) if s.latency else None,
                "sites": len(s.site_fails),
            })
        return rows

    def render(self):
        rows = self.stats_table()
        if not rows:
            return "代理池为空"
        out = [f"{'代理':<42} {'状态':<12} {'成功/失败':<12} {'延迟':<9} {'成功率'}"]
        out.append("-" * 88)
        for r in rows:
            lat = f"{r['latency_ms']}ms" if r["latency_ms"] else "-"
            out.append(f"{r['proxy']:<42} {r['state']:<12} "
                       f"{str(r['ok']) + '/' + str(r['fail']):<12} {lat:<9} "
                       f"{r['success_rate'] * 100:.0f}%")
        return "\n".join(out)
