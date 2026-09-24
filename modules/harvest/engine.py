#!/usr/bin/env python3
"""harvest 获取引擎 —— 后端调度、连接复用、限速、重试、降级

四个后端,按成本从低到高:

    static    纯 HTTP + 完整浏览器头 + Cookie 会话        ~0.2s
    embedded  同上,但优先从内嵌 JSON 取数据(不额外请求)   ~0.2s
    render    Playwright 无头渲染,等 networkidle          ~2s
    stealth   渲染 + 反检测注入(隐藏 webdriver 等特征)     ~3s

auto 模式由 scout 的判定驱动:先发一次静态请求,读响应特征,
只有在判定必须渲染时才升级 —— 而不是闭着眼睛轮流试一遍。

限制全部是显式参数,默认值见 PROFILES。默认不复用 urllib:
urllib 每次请求都重建 TCP+TLS,HTTPS 上单次握手 100-300ms,
那是吞吐的头号瓶颈,不是并发数。
"""

from __future__ import annotations

import gzip
import http.client
import http.cookiejar
import importlib.util
import random
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# 同目录模块,不走包导入 —— engine.py 是被 harvest.py 直接按文件加载的
sys.path.insert(0, str(Path(__file__).resolve().parent))
from proxypool import DEFAULT_CHECK_URL, ProxyPool      # noqa: E402
from memory import BackendMemory                        # noqa: E402

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# 轮换池。教程 02 章:固定 UA 会被关联分析,同一 UA 的大量请求是最好认的特征
UA_POOL = [
    BROWSER_UA,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
]

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# 开箱预设。默认 aggressive —— 限制都在,只是默认不启用
PROFILES = {
    "aggressive": {"rate": 0.0, "concurrency": 32, "respect_robots": False,
                   "rotate_ua": True, "keepalive": True, "retries": 2, "max_redirects": 10},
    "balanced": {"rate": 2.0, "concurrency": 8, "respect_robots": True,
                 "rotate_ua": False, "keepalive": True, "retries": 2, "max_redirects": 10},
    "conservative": {"rate": 0.5, "concurrency": 2, "respect_robots": True,
                     "rotate_ua": False, "keepalive": False, "retries": 1, "max_redirects": 5},
}
DEFAULT_PROFILE = "aggressive"

# 反检测注入:覆盖教程 05 章列出的 Level 1 基础检测点
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}, loadTimes: function(){}, csi: function(){}};
const _q = window.navigator.permissions && window.navigator.permissions.query;
if (_q) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q(p));
}
"""

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
BLOCKED_STATUS = {401, 403, 406, 418, 451}
REDIRECT_STATUS = {301, 302, 303, 307, 308}


def has_module(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# 渲染引擎优先用 patchright —— 它是 Playwright 的未检测版本,接口完全一致,
# 但抹掉了 CDP 特征与 navigator.webdriver 之类的自动化痕迹
PLAYWRIGHT_LIKE = ("patchright", "playwright")


def playwright_module():
    for name in PLAYWRIGHT_LIKE:
        if has_module(name):
            return name
    return None


def make_cffi_session(impersonate="chrome", proxies=None, cookies=None, timeout=15,
                      ca_file=None):
    """用 curl_cffi 建会话。

    urllib 的 TLS 指纹(Python 的 JA3)在风控面前是裸奔的 —— 换个库,
    让握手指纹跟真实 Chrome 一致,这一层的检测就直接绕过去了。
    """
    if not has_module("curl_cffi"):
        return None
    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate=impersonate, timeout=timeout,
                                        verify=str(ca_file) if ca_file else True)
        if proxies:
            session.proxies = {"http": proxies[0], "https": proxies[0]}
        for name, value in (cookies or {}).items():
            session.cookies.set(name, value)
        return session
    except Exception:
        return None


def available_backends():
    """探测本机可用的后端。

    这里列出的键必须与 fetch() 真正能分派的后端一致。曾经它恒报
    `"embedded": True`,而引擎里根本没有 embedded 的实现 —— `--check` 于是
    列出一个永远用不了的后端,按名字指定它会静默落回 auto 路径。
    """
    out = {"static": True}
    if has_module("curl_cffi"):
        out["impersonate"] = True
    if playwright_module():
        out["render"] = True
        out["stealth"] = True
    if has_module("camoufox"):
        out["camoufox"] = True
    if has_module("scrapling"):
        out["scrapling"] = True
    if has_module("ddddocr"):
        out["captcha"] = True
    return out


@dataclass
class Result:
    url: str
    final_url: str = ""
    status: int = 0
    backend: str = ""
    html: str = ""
    headers: dict = field(default_factory=dict)
    elapsed: float = 0.0
    retries: int = 0
    error: str = ""
    blocked: bool = False
    notes: list = field(default_factory=list)
    verdict: str = ""
    bytes: int = 0
    proxy: str = ""
    ua: str = ""


class RateLimiter:
    """按域名排队。rate <= 0 表示不限速(直接放行)。"""

    def __init__(self, rate=0.0):
        self.rate = float(rate)
        self._next = {}
        self._lock = threading.Lock()

    def acquire(self, url):
        if self.rate <= 0:
            return 0.0
        domain = urllib.parse.urlparse(url).netloc or "-"
        with self._lock:
            now = time.monotonic()
            nxt = self._next.get(domain, now)
            wait = max(0.0, nxt - now)
            self._next[domain] = max(nxt, now) + 1.0 / self.rate
        if wait > 0:
            time.sleep(wait)
        return wait


class ConnectionPool:
    """按 (scheme, host, port) 复用 TCP/TLS 连接。

    urllib 默认每次请求新建连接,HTTPS 上单次握手 100-300ms,
    高并发下这是吞吐的头号瓶颈 —— 不是并发数不够,是每次都重新握手。
    """

    def __init__(self, idle_ttl=90):
        self._idle = defaultdict(list)
        self._lock = threading.Lock()
        self.idle_ttl = idle_ttl
        self.opened = 0
        self.reused = 0

    def acquire(self, scheme, host, port, timeout, ssl_ctx):
        key = (scheme, host, port)
        now = time.monotonic()
        with self._lock:
            pool = self._idle.get(key) or []
            while pool:
                conn, stamp = pool.pop()
                if now - stamp < self.idle_ttl:
                    self.reused += 1
                    return conn
                try:
                    conn.close()
                except Exception:
                    pass
            self.opened += 1
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl_ctx)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def release(self, scheme, host, port, conn, reusable=True):
        if not reusable:
            try:
                conn.close()
            except Exception:
                pass
            return
        with self._lock:
            self._idle[(scheme, host, port)].append((conn, time.monotonic()))

    def close_all(self):
        with self._lock:
            for pool in self._idle.values():
                for conn, _ in pool:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._idle.clear()


class RobotsGate:
    """robots.txt 网关。默认关闭 —— 需要时用 respect_robots=True 打开。
    取不到 robots.txt 时按行业惯例放行。"""

    def __init__(self, enabled=False, user_agent="armory-harvest"):
        self.enabled = enabled
        self.user_agent = user_agent
        self._cache = {}

    def allowed(self, url, opener=None, timeout=8):
        if not self.enabled:
            return True
        parts = urllib.parse.urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._cache:
            robot = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(base + "/robots.txt",
                                             headers={"User-Agent": self.user_agent})
                # OpenerDirector 提供 open(), 模块入口才叫 urlopen()。
                open_url = opener.open if opener is not None else urllib.request.urlopen
                with open_url(req, timeout=timeout) as resp:
                    robot.parse(resp.read().decode("utf-8", "replace").splitlines())
            except Exception:
                robot = None
            self._cache[base] = robot
        robot = self._cache[base]
        return True if robot is None else robot.can_fetch(self.user_agent, url)


# 拦截页特征。判质量时只看开头,避免正文里讨论这些词的正常页面被误伤
# 判「像不像拦截页」的特征。措辞与 handoff 的 GATE_SIGNS / HUMAN_SIGNS 对齐。
#
# 这两份列表曾经分叉:engine 把「验证码登录」「扫码登录」当拦截特征,而 handoff
# 明确把它们归入 MODAL_SIGNS(只是前端画了个框,服务端内容照样给)。同一个页面
# 两个模块给出相反结论 —— 后果就是白白升级:实测某页 11.4s 里 10.9s 是空转,
# 全花在为一个并不存在的"拦截"启动浏览器上。
# 「请登录」也太宽:它同样匹配「请登录后查看」这类正常页面的页脚提示。
_BLOCK_HINTS = ("verify you are human", "checking your browser",
                "access denied", "unusual traffic", "just a moment",
                "请登录后", "请先登录")


def visible_text_len(html):
    """粗略的可见文本长度。

    判「静态结果够不够用」必须看这个而不是 HTML 大小 —— 一个 5.7KB 的页面
    可能 78% 是脚本、正文只有 96 字节(那正是需要渲染的信号),而 187KB 的页面
    可能有 8000 多字正文。
    """
    import re
    # `(?:</\1>|$)` 的 `|$` 不能省:被 max_body 截断或服务端截断的 HTML 里
    # <script> 没有闭合标签,少了兜底就会把整段脚本内容当成正文留下来。
    # 实测同一段内容:闭合时算 2 字,未闭合时算 32 字 —— 这个虚高值直接喂给
    # 1500 字阈值,会把「需要升级」误判成「静态已经够用」。
    body = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?(?:</\1>|$)", " ", html or "")
    return len(re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", body)).strip())


def decode_body(body, headers):
    """按 Content-Type 里的 charset 解码,别一律按 utf-8。

    GBK / GB2312 的老中文站很常见,硬按 utf-8 解会整篇乱码 —— 而 status 200、
    error 为空,看起来完全成功。乱码还会继续污染可见正文量(1500 字升级阈值)、
    scout 的判定与正文抽取,一路静默走偏,事后很难从现象反推成因。
    """
    import re
    ctype = str((headers or {}).get("content-type") or "").lower()
    m = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", ctype)
    if m:
        try:
            return body.decode(m.group(1), errors="replace")
        except (LookupError, TypeError):
            pass                      # 站点写了个不存在的 charset,退回 utf-8
    return body.decode("utf-8", errors="replace")


def response_quality(res):
    """给响应打个可比较的分:(是否像拦截页, 正文长度)。

    用来决定「升级后端后要不要用新结果替换原结果」。判定「需要渲染」只是启发式,
    静态路径很可能已经拿到完整内容 —— 盲目替换会把好结果换成更差的。

    第二键是**可见正文量**,不是 HTML 字节数。两者曾经不一致:跳过升级用正文量、
    替换判据用字节数,于是渲染后注入一堆框架标记(HTML 从 5KB 涨到 800KB、正文一字
    未增)会被判成「更好」而白白替换;反过来静态 HTML 里带大段内联 script 时,它
    又会错误地赢过正文更多的渲染结果。改用同一个度量后两条判据才可比。
    """
    if res is None or res.error:
        return (2, 0)
    html = res.html or ""
    head = html[:2000].lower()
    blocked = any(h in head for h in _BLOCK_HINTS)
    return (1 if blocked else 0, visible_text_len(html))


class Engine:
    def __init__(self, rate=0.0, timeout=15, retries=2, proxy=None, proxies=None,
                 respect_robots=False, user_agent=BROWSER_UA, rotate_ua=False,
                 keepalive=True, cookies=None, max_body=10_000_000, max_redirects=10,
                 impersonate="chrome", prefer_cffi=True, pool_enabled=True, settle_ms=800,
                 proxy_check_url=None, proxy_sticky=True, render_wait_ms=6000,
                 backend_memory=True, ca_file=None):
        self.timeout = timeout
        self.retries = retries
        self.max_body = max_body
        self.max_redirects = max_redirects
        self.rotate_ua = rotate_ua
        self.user_agent = user_agent
        self.keepalive = keepalive
        # 渲染时等「网络空闲」的预算。很多站点(长轮询/心跳/流式列表)永远到不了
        # networkidle,固定等满 6 秒等于每次白等 —— 实测 oschina 10 秒了内容还在长,
        # 但正文在 4 秒时其实已经拿全(12,373 字 / 7.17s,反而比 6 秒档又多又快)。
        # 等待只是缓冲,完整性由最终质量判定兜底。测过的曲线:1.2s→0 字、
        # 2s→436 字、3s→1426 字、4s→12373 字、6s→3794 字(站点自身波动很大)。
        self.render_wait_ms = render_wait_ms
        # 按域名记住哪个后端用过、哪个刚失败过 —— 详见 memory.py
        self.memory = BackendMemory(enabled=backend_memory)
        self.limiter = RateLimiter(rate)
        self.robots = RobotsGate(respect_robots, user_agent)
        self.conn_pool = ConnectionPool()
        self.stats = {"requests": 0, "bytes": 0, "errors": 0}

        self.proxies = list(proxies or [])
        if proxy:
            self.proxies.insert(0, proxy)
        self._proxy_idx = 0
        self._proxy_lock = threading.Lock()
        # 有池子就交给池子调度:检测、评分、剔除、站点绑定都由它管
        self.proxy_pool = (ProxyPool(self.proxies, check_url=proxy_check_url or DEFAULT_CHECK_URL,
                               timeout=timeout, sticky=proxy_sticky)
                     if (pool_enabled and self.proxies) else None)

        # 所有 HTTP 路径都必须验证证书链和主机名。私有 CA 用显式 PEM 文件,
        # 绝不通过 CERT_NONE / verify=False 提高表面成功率。浏览器使用自己的
        # 系统信任库,此 ca_file 仅用于 Python/curl HTTP 客户端。
        self.ca_file = str(Path(ca_file).expanduser().resolve()) if ca_file else None
        self.tls_verify = self.ca_file or True
        self.ssl_ctx = ssl.create_default_context(cafile=self.ca_file)

        # 代理模式走 urllib(连接池与代理叠加复杂,且代理下吞吐本就不是首要)
        self.jar = http.cookiejar.CookieJar()
        for name, value in (cookies or {}).items():
            self.jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain="", domain_specified=False, domain_initial_dot=False,
                path="/", path_specified=True, secure=False, expires=None,
                discard=True, comment=None, comment_url=None, rest={}, rfc2109=False))
        handlers = [urllib.request.HTTPCookieProcessor(self.jar),
                    # 两条路径必须共用同一个 TLS 策略:否则证书异常的目标会在
                    # 连接池路径成功、urllib 路径失败,行为不可预期
                    urllib.request.HTTPSHandler(context=self.ssl_ctx)]
        if self.proxies:
            handlers.append(urllib.request.ProxyHandler(
                {"http": self.proxies[0], "https": self.proxies[0]}))
        self.opener = urllib.request.build_opener(*handlers)

        # 首选 curl_cffi:握手层与真实 Chrome 一致,比自己维护连接池的收益大得多
        self.impersonate = impersonate
        self.settle_ms = settle_ms
        # 显式保存一份 cookie 字典。依赖 cookie jar 的 domain 匹配在 curl_cffi
        # 路径上不可靠 —— set(name, value) 不带 domain 时 cookie 可能根本不发出去,
        # 表现就是「登录态明明存了,请求过去还是登录页」
        self.cookies_dict = dict(cookies or {})
        self._cffi = (make_cffi_session(impersonate, self.proxies, cookies, timeout,
                                      ca_file=self.ca_file)
                      if prefer_cffi else None)
        self.backend_note = ("curl_cffi/" + impersonate if self._cffi else "builtin")

    # ---------------------------------------------------------- helpers

    def next_proxy(self, url=None):
        """选出口 IP。走代理池时按站点做会话亲和,避免同账号在多城市间瞬移。"""
        if self.proxy_pool is not None:
            site = urllib.parse.urlparse(url).netloc if url else None
            return self.proxy_pool.pick(site=site)
        if not self.proxies:
            return None
        with self._proxy_lock:
            proxy = self.proxies[self._proxy_idx % len(self.proxies)]
            self._proxy_idx += 1
        return proxy

    def report_proxy(self, proxy, ok, latency=None, site=None, banned=False):
        if self.proxy_pool is not None and proxy:
            self.proxy_pool.report(proxy, ok, latency=latency, site=site, banned=banned)

    def _pick_ua(self):
        return random.choice(UA_POOL) if self.rotate_ua else self.user_agent

    def _headers(self, ua=None):
        hdrs = dict(DEFAULT_HEADERS)
        hdrs["User-Agent"] = ua or self._pick_ua()
        return hdrs

    def _cookie_header(self, url):
        parts = urllib.parse.urlparse(url)
        pairs = []
        for c in self.jar:
            if c.domain and parts.netloc.endswith(c.domain.lstrip(".")) is False:
                continue
            pairs.append(f"{c.name}={c.value}")
        return "; ".join(pairs)

    def _store_cookies(self, url, headers):
        parts = urllib.parse.urlparse(url)
        raw = headers.get("set-cookie")
        if not raw:
            return
        for chunk in [raw] if isinstance(raw, str) else raw:
            head = chunk.split(";")[0]
            if "=" not in head:
                continue
            name, value = head.split("=", 1)
            try:
                self.jar.set_cookie(http.cookiejar.Cookie(
                    version=0, name=name.strip(), value=value.strip(), port=None,
                    port_specified=False, domain=parts.netloc, domain_specified=True,
                    domain_initial_dot=False, path="/", path_specified=True,
                    secure=parts.scheme == "https", expires=None, discard=True,
                    comment=None, comment_url=None, rest={}, rfc2109=False))
            except Exception:
                pass

    # ---------------------------------------------------------- keep-alive 路径

    def _request_pooled(self, url, method="GET", ua=None):
        """走连接池的请求 + 手动重定向 + 手动 Cookie。"""
        current = url
        for _ in range(self.max_redirects):
            parts = urllib.parse.urlparse(current)
            scheme = parts.scheme or "https"
            host = parts.hostname
            port = parts.port or (443 if scheme == "https" else 80)
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query

            headers = self._headers(ua)
            headers["Host"] = parts.netloc
            cookie = self._cookie_header(current)
            if cookie:
                headers["Cookie"] = cookie
            headers["Connection"] = "keep-alive"

            conn = self.conn_pool.acquire(scheme, host, port, self.timeout, self.ssl_ctx)
            try:
                conn.request(method, path, headers=headers)
                resp = conn.getresponse()
                body = resp.read(self.max_body)
                status = resp.status
                raw_headers = resp.getheaders()
                # read(max_body) 可能只读了一部分;未消费完的响应不能回池。
                # 不继续排空大响应,以免为复用连接突破响应体与等待预算。
                reusable = resp.isclosed() and not resp.will_close
            except Exception:
                self.conn_pool.release(scheme, host, port, conn, reusable=False)
                raise

            hdrs = {}
            set_cookie = []
            for k, v in raw_headers:
                k = k.lower()
                if k == "set-cookie":
                    set_cookie.append(v)
                else:
                    hdrs[k] = v
            if set_cookie:
                hdrs["set-cookie"] = set_cookie
            self.conn_pool.release(scheme, host, port, conn,
                              reusable=reusable
                              and hdrs.get("connection", "").lower() != "close")
            self._store_cookies(current, hdrs)

            if status in REDIRECT_STATUS and hdrs.get("location"):
                current = urllib.parse.urljoin(current, hdrs["location"])
                if status == 303:
                    method = "GET"
                continue
            return status, current, hdrs, body
        raise RuntimeError("重定向次数超过上限")

    # ---------------------------------------------------------- urllib 路径

    def _request_urllib(self, url, method="GET", proxy=None, ua=None):
        headers = self._headers(ua)
        req = urllib.request.Request(url, headers=headers, method=method)
        opener = self.opener
        if proxy and proxy != (self.proxies[0] if self.proxies else None):
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self.jar),
                urllib.request.HTTPSHandler(context=self.ssl_ctx),
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.url, dict(resp.headers), resp.read(self.max_body)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read(self.max_body)
            except Exception:
                pass
            return exc.code, (exc.url or url), dict(exc.headers or {}), body

    # ---------------------------------------------------------- curl_cffi 路径

    def _request_cffi(self, url, method="GET", ua=None, extra_headers=None):
        """握手指纹与真实浏览器一致的请求路径。

        UA 必须显式塞进 headers:curl_cffi 的 impersonate 对齐的是 TLS/HTTP2 特征,
        UA 仍是会话自己的默认值 —— 不覆盖的话 `rotate_ua` 在这条(装了 curl_cffi
        后就是默认的)路径上完全不起作用,而 res.ua 还记着一个根本没发出去的值。
        """
        headers = dict(extra_headers or {})
        if ua:
            headers["User-Agent"] = ua
        if self.cookies_dict:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies_dict.items())
        resp = self._cffi.request(method, url, headers=headers, timeout=self.timeout,
                                  allow_redirects=True, verify=self.tls_verify)
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        try:
            set_cookie = resp.headers.get_list("set-cookie")
            if set_cookie:
                hdrs["set-cookie"] = set_cookie
        except (AttributeError, TypeError):
            pass
        return resp.status_code, str(resp.url), hdrs, resp.content

    def _pick_path(self, proxy):
        """curl_cffi > 自建连接池 > urllib。前者握手最真,后两者是兜底。"""
        if self._cffi is not None and (not self.proxies or proxy == self.proxies[0]):
            return "cffi"
        if self.keepalive and not proxy:
            return "pool"
        return "urllib"

    # ---------------------------------------------------------- static

    def fetch_static(self, url, headers=None):
        res = Result(url=url, backend="static")
        # URL 合法性先过一遍。urlparse 对坏端口("http://a:99999/")和坏 IPv6
        # 会抛 ValueError,而那几处解析位于 try 之外 —— 异常会一路冒到调用方,
        # 深爬的 pool.map 又没有兜底,一个脏链接就能中断整轮。
        try:
            urllib.parse.urlparse(url).port
        except ValueError as exc:
            res.error = f"URL 非法: {exc}"
            return res

        attempt = 0
        # 整次抓取的计时,含重试与退避 —— 不是最后一次尝试的耗时。
        # 字段名与消费方(落盘记录、代理池延迟画像)都按"这次抓取花了多久"理解。
        started = time.time()
        while True:
            self.limiter.acquire(url)
            proxy = self.next_proxy(url)
            site = urllib.parse.urlparse(url).netloc
            res.proxy = proxy or ""
            ua = self._pick_ua()
            res.ua = ua
            try:
                path = self._pick_path(proxy)
                if path == "cffi":
                    status, final, hdrs, body = self._request_cffi(
                        url, ua=ua, extra_headers=headers)
                elif path == "pool":
                    status, final, hdrs, body = self._request_pooled(url, ua=ua)
                else:
                    status, final, hdrs, body = self._request_urllib(url, proxy=proxy, ua=ua)
            except (urllib.error.URLError, http.client.HTTPException, socket.timeout,
                    ssl.SSLError, OSError, RuntimeError, ValueError) as exc:
                self.stats["errors"] += 1
                self.report_proxy(proxy, ok=False, site=site)
                res.error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    attempt += 1
                    res.retries = attempt
                    time.sleep(min(2 ** attempt, 8))
                    continue
                return res

            # error 描述最终结果,重试历史仍由 retries 和 stats["errors"] 保留。
            res.error = ""
            hdrs = {k.lower(): v for k, v in hdrs.items()}
            encoding = (hdrs.get("content-encoding") or "").lower()
            if body:
                try:
                    if "gzip" in encoding:
                        body = gzip.decompress(body)
                    elif "deflate" in encoding:
                        try:
                            body = zlib.decompress(body, -zlib.MAX_WBITS)
                        except zlib.error:
                            # 一部分服务器发的是带 zlib 包装头的 deflate,
                            # 只试 raw 格式的话它们会稳定落进下面那个静默分支。
                            body = zlib.decompress(body)
                except Exception as exc:
                    # 解压失败不能静默 pass:压缩后的二进制会被当 HTML 解码成乱码,
                    # 而 status 200、error 为空,看起来完全成功 —— 正文量判定、
                    # scout 结论、正文抽取全在垃圾数据上跑。
                    res.notes.append(
                        f"解压失败({encoding}),保留原始字节: {type(exc).__name__}")

            self.stats["requests"] += 1
            self.stats["bytes"] += len(body)
            res.status, res.final_url, res.headers = status, final, hdrs
            res.bytes = len(body)
            res.html = decode_body(body, hdrs)
            res.elapsed = time.time() - started
            res.blocked = status in BLOCKED_STATUS

            if status in RETRYABLE_STATUS and attempt < self.retries:
                attempt += 1
                res.retries = attempt
                wait = self._retry_after(hdrs) or min(2 ** attempt, 8)
                res.notes.append(f"{status} 退避 {wait}s 重试")
                time.sleep(wait)
                continue
            # 拿到响应 = 代理连通;若被目标拒绝,记的是站点维度的账
            self.report_proxy(proxy, ok=True, latency=res.elapsed, site=site,
                              banned=status in BLOCKED_STATUS or status == 429)
            return res

    @staticmethod
    def _retry_after(headers):
        raw = headers.get("retry-after")
        if not raw:
            return None
        try:
            return min(float(raw), 30.0)
        except (ValueError, TypeError):
            return None

    # ---------------------------------------------------------- render

    def fetch_render(self, url, stealth=False, wait_until="networkidle"):
        res = Result(url=url, backend="stealth" if stealth else "render")
        mod = playwright_module()
        if not mod:
            res.error = ("未安装 playwright/patchright; "
                         "pip install patchright && patchright install chromium")
            return res
        self.limiter.acquire(url)
        try:
            sync_playwright = importlib.import_module(f"{mod}.sync_api").sync_playwright
        except Exception as exc:
            res.error = f"{mod} 导入失败: {exc}"
            return res
        # patchright 自带反检测,不用我们再补 stealth 脚本
        effective_stealth = stealth and mod == "playwright"
        res.notes.append(f"渲染引擎: {mod}" + ("(自带反检测)" if mod == "patchright" else ""))

        started = time.time()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox", "--disable-dev-shm-usage",
                ])
                context = browser.new_context(
                    ignore_https_errors=False,
                    user_agent=self.user_agent,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": 1440, "height": 900},
                    extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                )
                if effective_stealth:
                    context.add_init_script(STEALTH_JS)
                try:
                    page = context.new_page()
                    # networkidle 在 Firefox 内核下不可靠 —— 会提前返回,抓回一张空壳。
                    # 改成 domcontentloaded 打底 + 尽力等 networkidle + 固定沉降时间。
                    response = page.goto(url, wait_until="domcontentloaded",
                                         timeout=self.timeout * 1000)

                    # 等「网络空闲」,但给它一个预算而不是硬编码 6 秒。
                    # 能快速安静下来的站点(quotes.toscrape.com/js 实测 0.59s)立即返回;
                    # 有长轮询/心跳/新闻瀑布的站点永远到不了 networkidle,等满预算为止
                    # —— 那是站点本身的持续加载,不是我们等错了东西。
                    #
                    # 试过「内容够就走 / 内容稳定就走」的自适应(render_min_ms +
                    # render_ready_chars),实测**没有稳定收益**:oschina 首页正文在
                    # 1,646 / 2,859 / 5,237 / 12,373 / 37,287 字之间随机跳,那是它自己
                    # 先出骨架再持续追加的行为。任何中间态判据都是在抛硬币,反而让
                    # 总时长从 11.1s 涨到 12-13s。所以留下固定预算 + 三个可调参数,
                    # 不再自作聪明 —— 参数留给需要时手工调。
                    if self.render_wait_ms > 0 and wait_until not in ("domcontentloaded", "commit"):
                        try:
                            page.wait_for_load_state(wait_until, timeout=self.render_wait_ms)
                        except Exception:
                            pass
                    if self.settle_ms:
                        page.wait_for_timeout(self.settle_ms)
                    res.status = response.status if response else 0
                    res.final_url = page.url
                    res.html = page.content()
                    res.headers = {k.lower(): v for k, v in (response.headers if response else {}).items()}
                    res.bytes = len(res.html)
                finally:
                    # close 必须在 finally 里:渲染中途抛异常时,原先写在 try 末尾的
                    # close() 会被跳过,浏览器就留在后台一直占 CPU、一直不退出。
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        res.elapsed = time.time() - started
        res.blocked = res.status in BLOCKED_STATUS
        return res

    def fetch_scrapling(self, url):
        """Scrapling 适配。本机未装时不会走到这里;装了也做容错,API 变动不致命。"""
        res = Result(url=url, backend="scrapling")
        if not has_module("scrapling"):
            res.error = "未安装 scrapling"
            return res
        self.limiter.acquire(url)
        started = time.time()
        try:
            from scrapling.fetchers import StealthyFetcher
            # Scrapling 的 StealthySession 默认忽略证书错误,必须显式覆盖。
            page = StealthyFetcher.fetch(
                url, headless=True, network_idle=True,
                additional_args={"ignore_https_errors": False})
            html = ""
            for attr in ("html_content", "body", "text"):
                value = getattr(page, attr, None)
                if isinstance(value, str) and value.strip():
                    html = value
                    break
            if not html:
                html = str(page)
            res.html = html
            res.status = getattr(page, "status", 200) or 200
            res.final_url = getattr(page, "url", url) or url
            res.bytes = len(html)
        except Exception as exc:
            res.error = f"scrapling 调用失败: {str(exc)[:200]}"
        res.elapsed = time.time() - started
        return res

    def fetch_binary(self, url):
        """取原始字节(验证码图片、附件等)。沿用会话、代理、Cookie 与指纹。"""
        if self._cffi is not None:
            resp = self._cffi.get(url, timeout=self.timeout, allow_redirects=True,
                                  verify=self.tls_verify)
            return resp.status_code, resp.content
        req = urllib.request.Request(url, headers=self._headers())
        with self.opener.open(req, timeout=self.timeout) as resp:
            return resp.status, resp.read(5_000_000)

    # ---------------------------------------------------------- camoufox

    def fetch_camoufox(self, url, os_family="windows", humanize=True, persistent_dir=None):
        """指纹级反检测渲染(Firefox 内核)。

        patchright 抹掉的是 CDP 痕迹;这一层处理 WebGL / Canvas / 字体 / 插件 / 屏幕 ——
        实测 WebGL 报出真实显卡型号、plugins 有 5 个,而 Chromium 无头是 0 个。
        """
        res = Result(url=url, backend="camoufox")
        if not has_module("camoufox"):
            res.error = "未安装 camoufox; pip install camoufox && camoufox fetch"
            return res
        self.limiter.acquire(url)
        try:
            from camoufox.sync_api import Camoufox
        except Exception as exc:
            res.error = f"camoufox 导入失败: {exc}"
            return res

        started = time.time()
        proxy = self.next_proxy(url)
        site = urllib.parse.urlparse(url).netloc
        proxy_cfg = self._camoufox_proxy(proxy) if proxy else None

        kwargs = {"headless": True, "os": os_family, "humanize": bool(humanize),
                  "geoip": bool(proxy_cfg)}          # 有代理时让时区/语言与出口地区一致
        if proxy_cfg:
            kwargs["proxy"] = proxy_cfg
        if persistent_dir:
            # 设备指纹持久化:对付绑定设备的站点,同一 profile 反复用
            kwargs.update(persistent_context=True, user_data_dir=persistent_dir,
                          ignore_https_errors=False)

        try:
            with Camoufox(**kwargs) as browser:
                # 普通 browser 的 new_page 创建上下文;持久上下文已在上面设置。
                page = (browser.new_page() if persistent_dir else
                        browser.new_page(ignore_https_errors=False))
                response = page.goto(url, wait_until="domcontentloaded",
                                     timeout=self.timeout * 1000)
                try:
                    # 与 fetch_render 共用同一个渲染等待预算,别再硬编码一个数 ——
                    # 这两条路径的等待策略曾经不一致,调参只改一边等于没改。
                    page.wait_for_load_state("networkidle", timeout=self.render_wait_ms)
                except Exception:
                    pass                            # Firefox 下 networkidle 不可靠
                if self.settle_ms:
                    page.wait_for_timeout(self.settle_ms)
                res.status = response.status if response else 0
                res.final_url = page.url
                res.html = page.content()
                res.headers = {}
                res.bytes = len(res.html)
                res.notes.append(f"camoufox os={os_family}"
                                 + (",geoip" if proxy_cfg else "")
                                 + (",persistent" if persistent_dir else ""))
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        res.elapsed = time.time() - started
        res.blocked = res.status in BLOCKED_STATUS
        if proxy:
            self.report_proxy(proxy, ok=not res.error, latency=res.elapsed,
                              site=site, banned=res.blocked)
        return res

    @staticmethod
    def _camoufox_proxy(proxy_url):
        """把 http://user:pass@host:port 拆成 camoufox 要的字典。"""
        parts = urllib.parse.urlparse(proxy_url)
        cfg = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port or 80}"}
        if parts.username:
            cfg["username"] = urllib.parse.unquote(parts.username)
        if parts.password:
            cfg["password"] = urllib.parse.unquote(parts.password)
        return cfg

    # ---------------------------------------------------------- sniff

    def fetch_sniff(self, url, wait_ms=6000, use="camoufox", keep_bodies=6):
        """渲染页面并记录全部网络请求 —— 回答问题:数据到底从哪来。

        比静态扫 HTML 找接口线索可靠得多:页面真正调用的东西只有真跑一遍才知道。
        """
        sniffs = []
        engine_used = None
        try:
            if use == "camoufox" and has_module("camoufox"):
                from camoufox.sync_api import Camoufox
                opener, engine_used = Camoufox(headless=True, os="windows"), "camoufox"
            else:
                mod = playwright_module()
                if not mod:
                    return None, "既没有 camoufox 也没有 playwright/patchright"
                sync_playwright = importlib.import_module(f"{mod}.sync_api").sync_playwright
                opener, engine_used = sync_playwright(), mod

            with opener as handle:
                if engine_used == "camoufox":
                    browser, context = handle, None
                    page = browser.new_page(ignore_https_errors=False)
                else:
                    browser = handle.chromium.launch(headless=True,
                                                     args=["--disable-blink-features=AutomationControlled"])
                    context = browser.new_context(locale="zh-CN", ignore_https_errors=False)
                    page = context.new_page()

                def on_request(req):
                    sniffs.append({
                        "method": req.method, "url": req.url,
                        "type": req.resource_type,
                        "post_data": (req.post_data or "")[:400],
                    })

                def on_response(resp):
                    for item in reversed(sniffs):
                        if item["url"] == resp.url and "status" not in item:
                            item["status"] = resp.status
                            try:
                                item["size"] = len(resp.body())
                            except Exception:
                                item["size"] = None
                            break

                try:
                    page.on("request", on_request)
                    page.on("response", on_response)
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    except Exception as exc:
                        # 导航超时仍可保留已发生的请求,证书失败则不能伪装为空成功。
                        if any(marker in str(exc).upper() for marker in
                               ("ERR_CERT_", "SSL_ERROR_", "SEC_ERROR_", "CERTIFICATE")):
                            raise
                    page.wait_for_timeout(wait_ms)
                finally:
                    # 同上:wait_for_timeout 抛异常时不能把浏览器漏在后台
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as exc:
            return None, f"{type(exc).__name__}: {str(exc)[:160]}"
        return sniffs, engine_used

    @staticmethod
    def summarize_sniff(sniffs, only_api=True):
        """把请求清单整理成:域名分组 + 接口名 + 是否带 token/签名参数。"""
        import re as _re
        apis = []
        seen = set()
        for s in sniffs:
            url = s["url"]
            kind = s.get("type", "")
            if only_api and kind not in ("xhr", "fetch", "document") and "api" not in url:
                continue
            key = (s["method"], url.split("?")[0])
            if key in seen:
                continue
            seen.add(key)
            body = s.get("post_data") or ""
            fids = _re.findall(r'functionId["\']?\s*[:=]\s*["\']([\w.\-]+)["\']', body)
            fids += _re.findall(r'["\']?functionId["\']?=([\w.\-]+)', body)
            apis.append({
                "method": s["method"],
                "url": url,
                "host": urllib.parse.urlparse(url).netloc,
                "status": s.get("status"),
                "size": s.get("size"),
                "function_id": sorted(set(fids))[0] if fids else None,
                "has_body": bool(body),
                "body_sample": body[:200] if body else None,
            })
        return apis

    # ---------------------------------------------------------- auto

    def fetch(self, url, backend="auto", scout_mod=None, check_robots=True):
        """auto:静态起步 → 用 scout 判定 → 只在必要时升级后端。"""
        # robots 检查放在引擎里而不是 CLI 里:深爬会在引擎内产生成百个新 URL,
        # 只在入口查一次等于没查。默认关闭,respect_robots=True 时生效。
        if check_robots and not self.robots.allowed(url, self.opener):
            res = Result(url=url, error="robots.txt 不允许抓取")
            res.notes.append("已按 robots.txt 跳过")
            return res
        if backend == "static":
            return self.fetch_static(url)
        if backend == "render":
            return self.fetch_render(url, stealth=False)
        if backend == "stealth":
            return self.fetch_render(url, stealth=True)
        if backend == "scrapling":
            return self.fetch_scrapling(url)
        if backend == "camoufox":
            return self.fetch_camoufox(url)

        res = self.fetch_static(url)
        if res.error:
            return res

        if scout_mod is None:
            return res
        evidence = scout_mod.build_evidence(res.html, res.headers)
        verdict, why, _, _ = scout_mod.decide(evidence)
        res.verdict = verdict
        res.notes.append(f"侦察判定: {verdict} — {why}")

        need_render = verdict in ("动态渲染", "反检测渲染", "验证码闸门")
        need_stealth = verdict in ("反检测渲染", "验证码闸门")
        # 被拦截(403/429/503)时必须升级 —— 静态路径的握手指纹与浏览器特征
        # 就是被拦的原因,换个后端往往直接过。
        # 标成 hard_blocked 是因为它**不受下面 1500 字豁免的影响**:权限页/登录墙/
        # 限流页通常带全站导航与页脚,可见文本很容易超过阈值,豁免一旦生效就会
        # 原样返回一个被拦下来的页面,notes 里还留下「被目标拦截」和「跳过升级」
        # 两条互相矛盾的记录,对外表现为抓取成功。
        hard_blocked = res.blocked or res.status in (403, 429, 503)
        if hard_blocked:
            need_render = need_stealth = True
            res.notes.append(f"被目标拦截({res.status}),升级指纹级后端重试")

        # 但静态已经拿到实质内容时不做无谓升级:升级只是手段,内容到手了就别白跑。
        # 实测过这种浪费 —— 页面里带「验证码登录」字样(其实是登录弹窗)被判要渲染,
        # 结果白开两次浏览器,11.4s 里有 10.9s 是空转
        quality = response_quality(res)
        text_len = quality[1]        # 与 quality 同一份数据,别对全文正则跑两遍
        if need_render and not hard_blocked and quality[0] == 0 and text_len > 1500:
            res.notes.append(f"静态已拿到 {text_len} 字正文,跳过升级尝试")
            return res
        if need_render and text_len <= 1500:
            res.notes.append(f"静态正文仅 {text_len} 字,尝试升级后端")

        if need_render:
            candidates = []
            if need_stealth and has_module("camoufox"):
                candidates.append(("camoufox", lambda: self.fetch_camoufox(url)))
            if playwright_module():
                candidates.append(("playwright",
                                   lambda: self.fetch_render(url, stealth=need_stealth)))
            if has_module("scrapling"):
                candidates.append(("scrapling", lambda: self.fetch_scrapling(url)))

            if not candidates:
                res.notes.append("判定需要渲染,但本机没有可用渲染后端 —— 返回静态结果")
                return res

            # 按历史重排:上次在这域名上成功的排最前,冷却期内的排到最后。
            # 只调顺序不删 —— 站点会变,判死的后端留着才有机会被发现已经恢复。
            prior = self.memory.describe(url)
            if prior != "无记录":
                candidates = self.memory.rank(url, candidates)
                res.notes.append(f"后端历史: {prior}")

            for name, fetch_fn in candidates:
                upgraded = fetch_fn()
                if upgraded.error:
                    res.notes.append(f"{name} 升级失败: {upgraded.error[:70]}")
                    self.memory.record_fail(url, name, upgraded.status)
                    continue
                # 只有确实更好才替换 —— 静态路径可能已经拿到完整内容了
                if response_quality(upgraded) > response_quality(res):
                    upgraded.verdict = verdict
                    upgraded.retries = res.retries
                    upgraded.notes = res.notes + [f"已从静态升级到 {upgraded.backend}"]
                    self.memory.record_ok(url, name)
                    return upgraded
                res.notes.append(
                    f"升级到 {name} 未带来更好结果"
                    f"({visible_text_len(upgraded.html)} 字 vs {text_len} 字正文),保留原响应")
                self.memory.record_fail(url, name, upgraded.status)
            return res
        # 判定不需要升级时直接返回静态结果 —— 漏掉这个 return 会让整个函数返回 None
        return res


# ---------------------------------------------------------------- 异步路径


class AsyncEngine:
    """asyncio + curl_cffi 的抓取路径。

    线程池的上限是 GIL:每个线程都得抢解释器锁,并发加到 32 之后 QPS 反而下滑
    (bench 实测:零延迟回环上并发 1 跑 5322 QPS,加到 64 掉到 4493)。
    事件循环在一个线程里就能压住几百个连接 —— IO 密集场景这才是正解。

    渲染类后端(camoufox / playwright)仍是同步的,它们靠多进程而不是协程来扩展,
    所以这里只覆盖静态抓取路径。
    """

    def __init__(self, concurrency=64, timeout=15, impersonate="chrome",
                 proxies=None, cookies=None, retries=2, timeout_cap=40, rate=0.0,
                 ca_file=None):
        self.concurrency = max(int(concurrency), 1)
        self.timeout = timeout
        self.timeout_cap = timeout_cap
        self.impersonate = impersonate
        self.proxies = list(proxies or [])
        self.cookies = dict(cookies or {})
        self.ca_file = str(Path(ca_file).expanduser().resolve()) if ca_file else None
        self.tls_verify = self.ca_file or True
        self.retries = int(retries) if retries is not None else 0
        # 按域名限速。asyncio 是单线程的,这里不需要锁 —— 但必须用 await sleep,
        # 用 time.sleep 会把整个事件循环按死。
        self.rate = float(rate or 0.0)
        self._next = {}
        self.stats = {"requests": 0, "errors": 0, "bytes": 0, "retries": 0}

    async def _acquire(self, url):
        if self.rate <= 0:
            return
        import asyncio
        host = urllib.parse.urlparse(url).netloc
        now = time.monotonic()
        nxt = self._next.get(host, now)
        wait = max(0.0, nxt - now)
        self._next[host] = max(nxt, now) + 1.0 / self.rate
        if wait > 0:
            await asyncio.sleep(wait)

    async def _one(self, session, url, sem):
        import asyncio
        async with sem:
            await self._acquire(url)
            for attempt in range(self.retries + 1):
                attempt_started = time.time()
                try:
                    resp = await session.get(url, impersonate=self.impersonate,
                                             timeout=self.timeout, allow_redirects=True,
                                             verify=self.tls_verify)
                    content = resp.content or b""
                    self.stats["requests"] += 1
                    self.stats["bytes"] += len(content)
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    return Result(
                        url=url, final_url=str(resp.url), status=resp.status_code,
                        backend="async", html=decode_body(content, hdrs),
                        headers=hdrs, bytes=len(content),
                        # elapsed 与 blocked 不能留空:build_record 会把它们照原样
                        # 落盘,深爬记录于是全是 elapsed:0.0 / blocked:false,
                        # 从数据上看不出任何异常。
                        elapsed=time.time() - attempt_started,
                        blocked=resp.status_code in BLOCKED_STATUS,
                    )
                except Exception as exc:
                    if attempt >= self.retries:
                        self.stats["errors"] += 1
                        return Result(url=url, backend="async",
                                      error=f"{type(exc).__name__}: {str(exc)[:120]}")
                    self.stats["retries"] += 1
                    await asyncio.sleep(min(2 ** attempt, 8))
        return Result(url=url, backend="async", error="unreachable")

    async def fetch_many(self, urls):
        """并发抓一批。返回与入参同序的 Result 列表。"""
        import asyncio
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            return [Result(url=u, backend="async",
                           error="需要 curl_cffi: pip install curl_cffi") for u in urls]

        kwargs = {"timeout": self.timeout, "verify": self.tls_verify}
        if self.proxies:
            kwargs["proxy"] = self.proxies[0]
        # cookie 必须走显式请求头:curl_cffi 的 session.cookies.set(name, value)
        # 拿不到 domain,请求里根本不会带上去(同步 Engine 早先踩过同一个坑,
        # 那时只修了同步路径,深爬这条一直没 cookie)。
        if self.cookies:
            kwargs["headers"] = {
                "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        sem = asyncio.Semaphore(self.concurrency)
        # max_clients 是隐藏的并发上限:不显式设置的话,事件循环会卡在默认连接数上,
        # 实测 20ms RTT 下恒定 402 QPS(402 × 0.02s ≈ 8 个连接),加并发也不涨
        # 连接数不是越多越好:实测 2048 并发时内存涨到 269MB、QPS 反而掉到 1527
        # (线程池同条件下 7339 QPS / 117MB)。上限压在 256 更稳。
        max_clients = max(1, min(self.concurrency, 256))
        try:
            session = AsyncSession(max_clients=max_clients, **kwargs)
        except TypeError:
            session = AsyncSession(**kwargs)
        async with session as session:
            return await asyncio.gather(*[self._one(session, u, sem) for u in urls])

    def run(self, urls):
        """同步入口:内部起一次事件循环。"""
        import asyncio
        return asyncio.run(self.fetch_many(urls))

    def crawl(self, start_url, max_pages=50, same_host=True, scout_mod=None,
              extract_links=None, backend_picker=None, verbose=True):
        """并发 BFS 深爬。

        与同步版的关键差别:同一层的链接一次性并发抓,而不是一个一个等。
        瀑布式 BFS 在每层上都能吃满并发,层数越深优势越明显。
        """
        import asyncio
        from urllib.parse import urlparse

        async def _run():
            seen, frontier, results = {start_url}, [start_url], []
            host = urlparse(start_url).netloc
            while frontier and len(results) < max_pages:
                batch = frontier[:max_pages - len(results)]
                fetched = await self.fetch_many(batch)
                frontier = []
                for res in fetched:
                    results.append(res)
                    if res.error or not extract_links:
                        continue
                    for link in extract_links(res.html, res.final_url or res.url):
                        href = link["href"]
                        if href in seen:
                            continue
                        if same_host and urlparse(href).netloc != host:
                            continue
                        seen.add(href)
                        frontier.append(href)
                if verbose:
                    print(f"  [层 {len(results)}/{max_pages}] 新增 {len(frontier)} 条",
                          file=sys.stderr, flush=True)
            return results

        return asyncio.run(_run())
