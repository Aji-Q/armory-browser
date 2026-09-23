#!/usr/bin/env python3
"""handoff —— 人工接管:卡在验证那一步时,把控制权交回给你

自动化不该在验证码上硬耗:那是概率游戏,而且风控会记账。正确做法是检测到需要
人介入时暂停,等你处理完,程序接手继续跑。

三种接管形态,由强到弱:

    1. CDP 接管(推荐)  连到你正在用的浏览器。登录态、扩展、书签、"浏览器指纹"
                       全是你自己的 —— 验证顺手就过,而且这类流量在风控眼里
                       本来就是你的正常浏览。
    2. 持久化 profile   用指定的 profile 目录启动可见浏览器,数据留存在里面,
                       下次接着用。
    3. 有头窗口         程序自己开可见窗口,你在里面处理。

**关键性质**:程序断开连接不会关闭你的浏览器。接管结束、脚本退出,你的浏览器
照常在,登录态留在里面供下次使用。

判定「需要人工」的信号:
    - 页面命中验证码特征
    - 被 403/429 拦截且升级后端无效
    - 登录态失效又没有可用凭据
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scout"))

DEFAULT_PORTS = (9222, 9223, 9333)

ARMORY_HOME = Path.home() / ".armory"
BROWSER_PROFILE = ARMORY_HOME / "browser-profile"
# 测试与演示脚本用过的 profile —— 清理遗留进程时一并认领
EXTRA_PROFILES = ("/tmp/armory_handoff_demo",)

# 需要人工的信号。第一组只在标题与开头判定 —— 这些词正文里也可能出现
HUMAN_SIGNS = [
    ("verify you are human", "人机验证"),
    ("checking your browser", "浏览器检查"),
    ("just a moment", "等待跳转"),
    ("enable javascript and cookies", "要求 JS/Cookie"),
    ("请完成安全验证", "安全验证"),
    ("滑动验证", "滑块验证"),
    ("点击验证", "点选验证"),
    ("captcha", "验证码"),
    ("unusual traffic", "异常流量"),
    ("access denied", "访问拒绝"),
    ("请登录后", "登录墙"),
]

# 第一组:只说明「页面上有个浮层」,不代表内容取不到。命中这组**不判**需要人工 ——
# 知乎的登录弹窗就是典型,挂着弹窗内容照样读得到。
MODAL_SIGNS = [
    ("获取短信验证码", "登录弹窗"),
    ("验证码登录", "登录弹窗"),
    ("密码登录", "登录弹窗"),
    ("扫码登录", "扫码登录"),
    ("开通机构号", "登录弹窗"),
    ("第三方账号登录", "登录弹窗"),
]

# 第二组:说明「服务端把内容扣掉了」—— 这才是真需要人工的信号。
# 它和第一组的本质区别是:第一组是前端画了个框,第二组是后端没给你东西。
GATE_SIGNS = [
    ("登录后查看", "登录墙"),
    ("登录后继续", "登录墙"),
    ("登录以继续", "登录墙"),
    ("请先登录", "登录墙"),
    ("sign in to continue", "登录墙"),
    ("log in to continue", "登录墙"),
    ("login required", "登录墙"),
    ("subscribe to continue", "付费墙"),
    ("订阅后继续", "付费墙"),
    ("剩余内容需", "内容截断"),
    ("展开阅读全文", "内容折叠"),
    ("继续阅读全文", "内容折叠"),
]

READY_HINT = ("完成后程序会自动继续;也可以按 Ctrl-C 放弃")


def playwright_like():
    import importlib.util
    for name in ("patchright", "playwright"):
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except Exception:
            continue
    return None


def find_debug_browser(ports=DEFAULT_PORTS, timeout=1.0):
    """探测本机是否已有以调试模式运行的浏览器。返回 endpoint 或 None。"""
    for port in ports:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                        timeout=timeout) as resp:
                info = json.loads(resp.read())
            return {"endpoint": f"http://127.0.0.1:{port}", "port": port,
                    "browser": info.get("Browser", "?"),
                    "ws": info.get("webSocketDebuggerUrl", "")}
        except Exception:
            continue
    return None


def port_in_use(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def chromium_path():
    """拿到本地浏览器可执行文件。优先用已装的 playwright/patchright 的 chromium。"""
    mod = playwright_like()
    if mod:
        import importlib
        try:
            with importlib.import_module(f"{mod}.sync_api").sync_playwright() as pw:
                return pw.chromium.executable_path
        except Exception:
            pass
    # 退回系统里的真实浏览器 —— 接管自己的浏览器反而最自然
    for name, path in (
        ("Chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ("Chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ):
        if os.path.exists(path):
            return path
    for candidate in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def launch_debug_browser(profile_dir=None, port=9222, browser_path=None, verbose=True,
                         url="about:blank"):
    """以调试模式启动一个浏览器。返回 (Popen, endpoint)。

    用独立 profile 目录启动真实浏览器 —— 既保留你的扩展与设置,又不动你日常那份
    profile(浏览器占用 profile 时无法再启动)。
    """
    exe = browser_path or chromium_path()
    if not exe:
        raise RuntimeError("找不到浏览器可执行文件;用 --browser-path 指定")

    if profile_dir is None:
        profile_dir = BROWSER_PROFILE
    profile_dir = Path(profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)

    if port_in_use(port):
        raise RuntimeError(f"端口 {port} 已被占用;先用 --browser-info 看能否直接接管")

    cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
           "--no-first-run", "--no-default-browser-check", url]
    if verbose:
        print(f"[i] 启动浏览器: {Path(exe).name}", file=sys.stderr)
        print(f"    调试端口 {port} | profile {profile_dir}", file=sys.stderr)
    # start_new_session:给浏览器单开一个进程组。chromium 是多进程的,
    # 收尾时按进程组整组杀,才不会留下一堆孤儿渲染进程吃 CPU。
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)

    for _ in range(50):
        info = find_debug_browser(ports=(port,), timeout=1.0)
        if info:
            if verbose:
                print(f"[i] 已就绪: {info['browser']}", file=sys.stderr)
            return proc, info["endpoint"]
        time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"浏览器启动了但调试端口 {port} 未就绪")


def armory_browser_pids():
    """列出所有 armory 起的浏览器进程 PID(按 user-data-dir 认领)。

    自己起的调试浏览器、测试脚本用的演示 profile 都算我们的。
    用户日常那个浏览器不在其列 —— 它的 user-data-dir 不在这几个路径上。
    """
    profiles = [str(BROWSER_PROFILE)] + list(EXTRA_PROFILES)
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        if "--user-data-dir=" not in line:
            continue
        if not any(p in line for p in profiles):
            continue
        pid = line.strip().split(None, 1)[0]
        if pid.isdigit():
            pids.append(int(pid))
    return pids


def kill_stale_browsers(verbose=True):
    """收掉所有 armory 起的浏览器进程,返回清掉的进程组数。

    只开不关会攒出几十个 chromium 进程(每个还是多进程组),CPU 就是被它们拖住的。
    按进程组整组杀,才不会留下孤儿渲染进程。
    """
    import signal
    pids = armory_browser_pids()
    if not pids:
        if verbose:
            print("[i] 没有 armory 起的浏览器进程", file=sys.stderr)
        return 0
    groups = set()
    for pid in pids:
        try:
            groups.add(os.getpgid(pid))
        except Exception:
            groups.add(pid)

    for gid in groups:
        try:
            os.killpg(gid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(gid, signal.SIGTERM)
            except Exception:
                pass
    time.sleep(0.8)
    for pid in pids:
        try:
            os.kill(pid, 0)              # 还在就补一刀
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    if verbose:
        print(f"[i] 已清理 {len(pids)} 个进程 / {len(groups)} 个进程组", file=sys.stderr)
    return len(groups)


def write_secret(path, text):
    """原子写 + 0600。

    存档里是会话凭证 —— 会话 cookie 是过了密码和两步验证之后才发的,拿到就等于
    两个都绕过了。权限位只能挡住别的本地账户(同身份运行的进程照样能读),
    但至少别让它成为家目录里最好拿的那份凭证副本。

    原子写是防写入中断留下半截 JSON —— 那种文件下次加载会直接报错。
    """
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def default_state_path(url):
    """按域名归档的登录态路径。

    接管一次,这个域名以后都能用 —— 人工过完验证拿到的会话不该只用一次。
    """
    import urllib.parse
    host = urllib.parse.urlparse(url).netloc.replace(":", "_") or "unknown"
    d = Path.home() / ".armory" / "states"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{host}.json"


MAX_STATE_AGE_DAYS = 30


def state_age_days(payload):
    """存档有多旧。返回 None 表示没有时间戳。"""
    saved = (payload.get("_armory") or {}).get("saved_at")
    if not saved:
        return None
    return (time.time() - saved) / 86400


def credential_expired(payload):
    """凭证是不是彻底过期了。返回 (是否过期, 最晚的过期时间)。

    判据用**最晚**的过期时间,不是最早的。一份存档里总有几个短命辅助 cookie
    —— 知乎的 unlock_ticket 只有 30 分钟、BEC 1 小时 —— 而真正的登录凭证
    z_c0 有 180 天。按最早的算,存档在存下半小时后就会被判「已过期」而整份
    拒用,实测就是这么撞上的。

    离线这层只负责挡掉明显死透的存档;精确判定交给 auth.probe_state() 的
    在线探测,那才是能真正回答「这个凭证还认不认」的东西。

    只统计**正数** expires —— 会话级 cookie 的 expires 是 -1/0,会被解析成
    1969 年,算进去的话每个含 session cookie 的存档都会被误判成已过期。
    """
    meta = payload.get("cookie_meta") or {}
    now = time.time()
    expiries = [m["expires"] for m in meta.values()
                if isinstance(m, dict) and isinstance(m.get("expires"), (int, float))
                and m["expires"] > 0]
    if not expiries:
        return False, None
    latest = max(expiries)
    return latest < now, latest


def load_domain_state(url, max_age_days=MAX_STATE_AGE_DAYS):
    """取该域名已存档的登录态。返回 (cookies 字典, path 或 None)。

    两道闸口:
      - 存档超过 max_age_days → 拒绝加载(会话能活几个月,但这个文件不该无限期有效)
      - 凭证 cookie 已过期 → 拒绝加载

    要同时认两种存档格式:handoff 存的是 {name: value} 字典,playwright 导出的
    storage_state 是 [{"name":..., "value":...}] 列表。只认一种就会在另一种上崩。
    """
    path = default_state_path(url)
    if not path.is_file():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, None

    age = state_age_days(payload)
    if age is not None and age > max_age_days:
        return {}, path
    expired, _ = credential_expired(payload)
    if expired:
        return {}, path

    raw = payload.get("cookies")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}, path
    cookies = {}
    for item in (raw or []):
        if isinstance(item, dict) and item.get("name"):
            cookies[item["name"]] = item.get("value", "")
    return cookies, path


def state_status(path):
    """给人看的存档状态 —— 存档不加载时得说清为什么。"""
    path = Path(path)
    if not path.is_file():
        return f"没有存档: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"存档损坏: {path.name} ({exc})"
    age = state_age_days(payload)
    expired, latest = credential_expired(payload)
    n = len(payload.get("cookies") or {})
    bits = [f"{n} 项 cookie"]
    if age is not None:
        bits.append(f"{age:.1f} 天前保存")
    if latest:
        left = (latest - time.time()) / 86400
        # 不足一天时按小时说 —— 四舍五入成「0 天后过期」看不出是快过期了
        if expired:
            when = "已过期"
        elif left < 1:
            when = f"{left * 24:.1f} 小时后过期"
        else:
            when = f"{left:.0f} 天后过期"
        bits.append(f"存档 {when}")
        # 短命辅助 cookie(知乎的 unlock_ticket 只有 30 分钟)会先到期,
        # 但它们是可再生的,不影响凭证 —— 说出来免得看着像整份存档快废了
        meta = payload.get("cookie_meta") or {}
        exp = sorted(m["expires"] for m in meta.values()
                     if isinstance(m, dict) and isinstance(m.get("expires"), (int, float))
                     and m["expires"] > 0)
        if exp:
            short_left = (exp[0] - time.time()) / 86400
            if short_left * 24 < 24 and short_left > 0:
                bits.append(f"(另有辅助 cookie {short_left * 24:.1f} 小时后到期,可再生)")
    if age is not None and age > MAX_STATE_AGE_DAYS:
        bits.append(f"**超过 {MAX_STATE_AGE_DAYS} 天上限,不会加载**")
    return " | ".join(bits)


def strip_scripts(html):
    """去掉 script/style 之后的正文 HTML。

    判「页面有没有弹窗特征」必须扫这个而不是全文 —— 内联 JS 字符串里出现
    「验证码登录」也会被当成有弹窗,那是假阳性。
    """
    import re
    return re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html or "")


def visible_text_len(html):
    """粗略的可见文本长度。

    实现委托给 engine ——「正文量」是跨模块的共同判据:engine 用它决定要不要升级
    后端,handoff 用它判断内容有没有被挡住。两边各留一份实现,同一个页面就会算出
    两个长度,判定随之分叉。实测两份在常见页面上结果相同,但过滤的标签集合并不一致
    (一份含 template、一份走 strip_scripts),属于迟早会咬人的重复。

    延迟导入:handoff 不依赖 engine 的其余部分,不必在模块加载期就把它拉进来。
    """
    from engine import visible_text_len as _impl
    return _impl(html)


def need_human(html, status=None, url="", content_ok=None):
    """页面是否卡在需要人工的环节。返回 (是否, 原因)。

    **判据是「内容有没有被扣」,不是「页面上有没有弹窗」。**
    浮层(MODAL_SIGNS)只说明前端画了个框,服务端照样把内容给了你 ——
    知乎的登录弹窗就是这种,判成需要人工会让全自动流程白等五分钟。

    真需要人工的信号只有两类:
      1. 服务端扣内容 —— GATE_SIGNS(登录后查看 / 展开全文 / 订阅后继续)
      2. 明确的拦截状态码 —— 403/429/503

    content_ok: 调用方知道目标内容取没取到时传进来(比如 --css 的命中数)。
      它会压过所有启发式 —— 这正好回答「被挡住的恰好是我要的那部分」:
      要的部分能抽到就说明没被挡。
    """
    if content_ok is True:
        return False, ""
    if content_ok is False:
        return True, "指定内容未取到"

    stripped = strip_scripts(html)
    low = stripped.lower()

    # 服务端扣内容的信号 —— 在正文里扫(已去掉脚本,避免 JS 字符串误伤)
    for sig, name in GATE_SIGNS:
        if sig.lower() in low:
            return True, name

    # 这几条只看标题与开头 —— 正文里讨论「验证码」的正常页面不该被误判
    title = ""
    if html:
        import re
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = (m.group(1) if m else "")[:200]
    probe = (title + " " + stripped[:1500]).lower()
    for sig, name in HUMAN_SIGNS:
        if sig in probe:
            return True, name

    if status in (403, 429, 503):
        return True, f"被拦截 HTTP {status}"

    # 只命中浮层特征时,看页面到底有没有正文:
    #   有正文 → 弹窗只是浮层,内容读得到,不打扰人(知乎那种)
    #   没正文 → 纯登录页,确实被挡在门外
    modal_hit = next((name for sig, name in MODAL_SIGNS if sig.lower() in low), None)
    if modal_hit:
        text_len = visible_text_len(stripped)
        if text_len < 500:
            return True, f"{modal_hit},且页面无正文({text_len} 字)"
        return False, ""

    return False, ""


def wait_for_human(page, ready_fn, reason="", timeout=300, poll=5.0, verbose=True,
                   on_tick=None):
    """挂一个 watchdog 等人工处理,直到 ready_fn 为真或超时。

    poll 默认 5 秒 —— 人工操作是分钟级的,探太勤只会白烧 CPU。
    **超时不算失败**:调用方应当降级回全自动,而不是把整个任务卡死。
    人不在的时候,脚本该自己往下走。
    """
    if verbose:
        bar = "=" * 64
        print(f"\n{bar}", file=sys.stderr)
        print(f"  ⏸  已暂停: {reason}", file=sys.stderr)
        print(f"  请在浏览器窗口里完成处理。每 {int(poll)}s 检测一次,最多等 {timeout}s。",
              file=sys.stderr)
        print(f"  超时会自动放弃人工方式、降级回全自动继续 —— 你不在也不耽误",
              file=sys.stderr)
        print(f"{bar}\n", file=sys.stderr)
    deadline = time.time() + timeout
    started = time.time()
    waited = 0.0
    while time.time() < deadline:
        try:
            if ready_fn(page):
                if verbose:
                    print(f"[i] ✓ {reason} 已处理完(等了 {int(time.time() - started)}s),继续抓取",
                          file=sys.stderr)
                return True
        except Exception:
            pass                      # 判定异常不能让 watchdog 死等
        time.sleep(poll)
        # 用实际耗时,不要 waited += poll —— sleep 会被调度延迟拉长,累加值偏小,
        # 日志里就会出现「等待中 15s / 12s」这种超过上限的读数。
        waited = time.time() - started
        if verbose:
            print(f"    ⏳ 等待中 {int(waited)}s / {timeout}s", file=sys.stderr)
        if on_tick:
            try:
                on_tick(waited)
            except Exception:
                pass                  # 回调是给调用方挂的,它出错不该拖垮等待本身
    if verbose:
        print(f"[i] ⏱ 等满 {timeout}s 仍未完成,降级回全自动", file=sys.stderr)
    return False


class BrowserSession:
    """一次接管会话。连别人的浏览器,或自己开一个。"""

    def __init__(self, cdp=None, profile=None, headless=False, port=9222,
                 allowed_low_ports=False, verbose=True, keep_alive=False):
        self.cdp = cdp
        self.profile = profile
        self.headless = headless
        self.port = port
        self.verbose = verbose
        self.keep_alive = keep_alive
        self._proc = None
        self._pw = None
        self._browser = None
        self._owns_browser = False
        self._own_pages = []          # 本会话开的 tab,收尾时要能全关掉
        # Playwright 默认拒绝连接低位端口,接管自己的浏览器时要放开
        self._allowed_low_ports = allowed_low_ports

    def __enter__(self):
        mod = playwright_like()
        if not mod:
            raise RuntimeError("接管需要浏览器引擎: pip install patchright")
        import importlib
        self._pw = importlib.import_module(f"{mod}.sync_api").sync_playwright().start()

        endpoint = self.cdp
        if not endpoint:
            found = find_debug_browser()
            if found:
                endpoint = found["endpoint"]
                if self.verbose:
                    print(f"[i] 发现可接管的浏览器: {found['browser']} @ {endpoint}", file=sys.stderr)
            else:
                self._proc, endpoint = launch_debug_browser(
                    profile_dir=self.profile, port=self.port, verbose=self.verbose)
                self._owns_browser = True

        self.endpoint = endpoint
        try:
            self._browser = self._pw.chromium.connect_over_cdp(
                endpoint, no_defaults=True, slow_mo=None)
        except TypeError:
            # 旧版本没有 no_defaults 参数
            self._browser = self._pw.chromium.connect_over_cdp(endpoint)
        if self.verbose:
            print(f"[i] 已接管(endpoint {endpoint})", file=sys.stderr)
        return self

    def _new_page(self):
        """开一个 tab 并记账。收尾时要能全部关掉,否则 tab 会一直堆在浏览器里。"""
        ctx = self._browser.contexts[0] if self._browser.contexts \
            else self._browser.new_context()
        page = ctx.new_page()
        self._own_pages.append(page)
        return page

    def page(self, new=False):
        if new or not self._browser.contexts or not self._browser.contexts[0].pages:
            return self._new_page()
        return self._browser.contexts[0].pages[0]

    def goto(self, url, wait_until="domcontentloaded", timeout=45):
        page = self._new_page()
        page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
        return page

    def cookies(self, with_meta=False):
        """取会话 cookie。

        with_meta=True 时带上 expires/domain —— 存档只留 name→value 会把过期时间
        丢掉,而那正是判断「这个文件还能用多久」的唯一依据。
        """
        out, meta = {}, {}
        for ctx in self._browser.contexts:
            for c in ctx.cookies():
                name = c.get("name")
                if not name:
                    continue
                out[name] = c.get("value", "")
                meta[name] = {"expires": c.get("expires"), "domain": c.get("domain"),
                              "path": c.get("path"), "secure": c.get("secure"),
                              "httpOnly": c.get("httpOnly")}
        return (out, meta) if with_meta else out

    def __exit__(self, *exc):
        # 1) 关掉本会话开的 tab。接管模式下浏览器是用户的 —— 每个 tab 都是一个
        #    独立的渲染进程,只开不关会让 CPU 一直背着它们。这是实测出来的:
        #    反复跑接管抓取,用户浏览器里就堆起了一串标签页。
        for page in self._own_pages:
            try:
                page.close()
            except Exception:
                pass
        self._own_pages.clear()

        # 2) 断开调试连接。用户的浏览器保持运行,只是不再由我们控制。
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

        # 3) 自己启动的浏览器默认关掉。以前这里只打印一句「保留运行中」,
        #    于是每次调用都留下一个浏览器进程 —— 跑几十次就是几十个。
        if self._owns_browser and self._proc:
            if self.keep_alive:
                if self.verbose:
                    print("[i] 浏览器按 --keep-browser 保留运行中", file=sys.stderr)
            else:
                self._close_owned_browser()

    def _close_owned_browser(self):
        """收掉本次自己启动的浏览器(整棵进程树)。

        chromium 是多进程的:只 terminate 主进程,渲染进程会变成孤儿继续吃 CPU。
        所以按进程组整组收 —— 这需要 Popen 用 start_new_session 单独开局。
        """
        import signal
        proc = self._proc
        try:
            if not proc or proc.poll() is not None:
                return
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
            if self.verbose:
                print("[i] 已关闭本次启动的浏览器", file=sys.stderr)
        except Exception:
            pass

    def close_page(self, page):
        """单独关一个 tab(调用方提前用完时)。"""
        try:
            page.close()
        except Exception:
            pass
        try:
            self._own_pages.remove(page)
        except ValueError:
            pass
        return False


def handoff_fetch(url, ready_fn=None, timeout=300, profile=None, cdp=None,
                  headless=False, save_state=None, verbose=True, settle=1.5,
                  keep_alive=False):
    """接管式抓取:打开页面 → 卡住就等你 → 拿到内容后返回。

    ready_fn(page) 为真表示"可以继续了";默认判据是页面不再命中人工信号。
    """
    with BrowserSession(cdp=cdp, profile=profile, headless=headless,
                        verbose=verbose, keep_alive=keep_alive) as session:
        # 打开页面与取内容都会抛(导航超时、页面崩溃、标签被关)。让异常冒出去等于
        # 调用方拿到一个 traceback 而不是它约定的 dict,于是"接管失败"和"程序出错"
        # 混成一种表现。这里一律转成结构化返回。
        try:
            page = session.goto(url)
        except Exception as exc:
            return {"ok": False, "url": url, "html": "",
                    "reason": f"打开页面失败: {type(exc).__name__}: {str(exc)[:120]}"}
        time.sleep(settle)
        try:
            html = page.content()
        except Exception as exc:
            return {"ok": False, "url": str(getattr(page, "url", url)), "html": "",
                    "reason": f"取页面内容失败: {type(exc).__name__}"}
        blocked, reason = need_human(html, url=page.url)

        if blocked:
            last_probe = {"why": "尚未开始探测"}

            def default_ready(p):
                # 整体包住:任何一处抛异常都会让判定永远返回 False,而外层又把异常
                # 吞了 —— 那就成了「用户明明过了验证,程序还在干等」这种最难查的故障
                try:
                    try:
                        if p.is_loading():
                            last_probe["why"] = "页面加载中"
                            return False
                    except Exception:
                        pass
                    try:
                        cur = p.content()
                    except Exception as exc:
                        last_probe["why"] = f"取内容失败({type(exc).__name__}: {exc})"
                        return False
                    # 门槛压得很低:极简页面(API 响应、短 HTML)可能只有一两百字节,
                    # 用长度当「是否加载完」的判据会把它们误杀成「还在加载」。
                    # 防误判靠的是后面的终检,不是这个长度。
                    if len(cur) < 50:
                        last_probe["why"] = f"内容几乎为空({len(cur)}B)"
                        return False
                    try:
                        cur_url = p.url
                    except Exception:
                        cur_url = ""
                    still, why = need_human(cur, url=cur_url)
                    last_probe["why"] = f"仍命中「{why}」" if still else "验证已通过"
                    return not still
                except Exception as exc:
                    last_probe["why"] = f"判定异常: {type(exc).__name__}: {str(exc)[:80]}"
                    return False

            check = ready_fn or default_ready
            if not wait_for_human(page, check, reason=reason, timeout=timeout,
                                  verbose=verbose):
                # 超时不是失败 —— 降到「当前拿到什么就用什么」,别把任务卡死。
                # 无人值守时这才是正确行为。
                try:
                    html = page.content()
                except Exception:
                    html = ""
                return {"ok": True, "degraded": True, "url": page.url, "html": html,
                        "cookies": session.cookies(), "handled": True,
                        "reason": f"等待超时({reason}),已降级为当前内容"}
            time.sleep(settle)
            html = page.content()

            # 终检:接管"结束"不等于验证真的过了,再确认一次,别把验证页当数据
            still_blocked, still_reason = need_human(html, url=page.url)
            if still_blocked:
                return {"ok": False,
                        "reason": f"接管后仍卡在验证({still_reason})", "url": page.url,
                        "html": html}

        cookies, cookie_meta = session.cookies(with_meta=True)
        result = {"ok": True, "url": page.url, "html": html,
                  "cookies": cookies, "cookie_meta": cookie_meta,
                  "handled": blocked, "reason": reason if blocked else ""}
        if save_state:
            write_secret(save_state,
                         json.dumps({"cookies": cookies, "cookie_meta": cookie_meta,
                                     "_armory": {"from": "handoff", "url": page.url,
                                                 "saved_at": time.time()}},
                                    ensure_ascii=False, indent=2))
            if verbose:
                print(f"[i] 接管期间的 Cookie 已存: {save_state}"
                      f"({len(result['cookies'])} 项)", file=sys.stderr)
        return result


def main():
    import argparse
    ap = argparse.ArgumentParser(description="人工接管:把验证步骤交给用户")
    ap.add_argument("url", nargs="?", help="目标 URL")
    ap.add_argument("--browser-info", action="store_true", help="看有没有可接管的浏览器")
    ap.add_argument("--browser-start", action="store_true", help="以调试模式启动浏览器")
    ap.add_argument("--browser-path", help="浏览器可执行文件路径")
    ap.add_argument("--port", type=int, default=9222, help="调试端口(默认 9222)")
    ap.add_argument("--profile", help="profile 目录(默认 ~/.armory/browser-profile)")
    ap.add_argument("--cdp", help="直接指定 CDP endpoint")
    ap.add_argument("--headless", action="store_true", help="无头(接管场景通常不需要)")
    ap.add_argument("--timeout", type=int, default=300, help="等待人工的秒数")
    ap.add_argument("--save-state", help="把接管期间的 Cookie 存成状态文件")
    args = ap.parse_args()

    if args.browser_info:
        found = find_debug_browser()
        if found:
            print(f"可接管: {found['browser']}")
            print(f"  endpoint: {found['endpoint']}")
            print(f"  用 --cdp {found['endpoint']} 直接接管")
        else:
            print("没有发现以调试模式运行的浏览器。")
            print(f"  可以 `--browser-start` 起一个,或手动:")
            exe = chromium_path() or "<浏览器路径>"
            print(f"  {exe} --remote-debugging-port=9222 "
                  f"--user-data-dir=~/.armory/browser-profile")
        return 0

    if args.browser_start:
        proc, endpoint = launch_debug_browser(profile_dir=args.profile, port=args.port,
                                              browser_path=args.browser_path)
        print(f"\n浏览器已启动,调试端口 {endpoint}")
        print("现在可以在里面登录、过验证,然后:")
        print(f"  python3 {sys.argv[0]} <url> --cdp {endpoint}")
        return 0

    if not args.url:
        ap.error("需要 URL(或用 --browser-info / --browser-start)")

    result = handoff_fetch(args.url, timeout=args.timeout, profile=args.profile,
                           cdp=args.cdp, headless=args.headless,
                           save_state=args.save_state)
    if result["ok"]:
        print(f"\n拿到内容: {len(result['html'])} 字节 | "
              f"{'经过人工接管' if result['handled'] else '无需人工'}")
        if result["handled"]:
            print(f"(触发原因: {result['reason']})")
    else:
        print(f"\n[!] {result['reason']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
