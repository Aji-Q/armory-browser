#!/usr/bin/env python3
"""harvest 登录模块 —— 用浏览器过一次登录,把凭证交给静态后端

思路:登录是低频、复杂、易变的一步(可能有 CSRF、JS 加密、验证码、扫码);
抓取是高频、简单、要求快的一步。把两件事拆开 ——

    浏览器登录一次 → 导出 storage_state → 提取 cookie 注入静态后端
    之后所有抓取走静态路径,实测 0.12s/页,比每次渲染快约 16 倍

Cookie 会过期。状态文件里记了签发时间,load 时可检查年龄。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# 登录成功的页面特征(中英)
LOGGED_IN_HINTS = ["logout", "sign out", "log out", "退出登录", "注销", "我的账户", "my account"]
# 登录失败的页面特征。必须用完整短语 —— 裸 "invalid" 会命中登录页 JS 里的
# 表单校验文案,把成功页判成失败
LOGIN_FAIL_HINTS = [
    "invalid credentials", "invalid username", "invalid password", "incorrect password",
    "incorrect username", "login failed", "authentication failed", "bad credentials",
    "用户名或密码错误", "账号或密码错误", "密码错误", "登录失败", "验证码错误", "认证失败",
]

USER_FIELD_SELECTORS = [
    "input[name='username']", "input[id='username']", "input[name='user']",
    "input[name='email']", "input[id='email']", "input[type='email']",
    "input[name='login']", "input[name='account']", "input[name*='user']",
    "input[type='text']",
]
PASS_FIELD_SELECTORS = ["input[type='password']", "input[name='password']", "input[id='password']"]
SUBMIT_SELECTORS = [
    "button[type='submit']", "input[type='submit']",
    "button:has-text('登录')", "button:has-text('登入')", "button:has-text('Login')",
    "button:has-text('Sign in')",
]


class LoginError(RuntimeError):
    pass


def playwright_like():
    """优先 patchright —— 必须与 engine 用同一个引擎,否则两边浏览器不共享,
    一个能启动另一个报 "Executable doesn't exist"。"""
    import importlib.util
    for name in ("patchright", "playwright"):
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except Exception:
            continue
    return None


def has_playwright():
    return playwright_like() is not None


def _first(page, selectors):
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:
            continue
        if el and el.is_visible():
            return el, sel
    return None, None


def detect_login_state(html, url_before, url_after):
    """登录是否成功。判定顺序:失败提示 → 登出特征 → URL 是否离开登录页。

    失败提示优先 —— 页面明说凭据无效,那不管跳到哪这次都没成功。
    """
    low = html.lower()
    if any(h in low for h in LOGIN_FAIL_HINTS):
        return False, "页面出现登录失败提示"
    if any(h in low for h in LOGGED_IN_HINTS):
        return True, "页面出现登出/账户特征"
    changed = url_after.split("?")[0].rstrip("/") != url_before.split("?")[0].rstrip("/")
    if changed and not any(k in url_after.lower() for k in ("login", "signin", "auth")):
        return True, f"已从登录页跳转到 {url_after}"
    return False, "未能确认登录成功"


def login(login_url, user, password, state_path, headless=True, timeout=30,
          user_field=None, pass_field=None, submit_field=None, wait_after=2.0,
          keep_open=0.0, verbose=True):
    """打开登录页 → 填表 → 提交 → 判定结果 → 保存 storage_state。

    keep_open > 0 时提交后保持浏览器打开若干秒,留给人工处理验证码/扫码。
    """
    if not has_playwright():
        raise LoginError("登录需要浏览器引擎: pip install patchright && patchright install chromium")

    import importlib
    mod = playwright_like()
    sync_playwright = importlib.import_module(f"{mod}.sync_api").sync_playwright

    log = (lambda m: print(f"  {m}", file=sys.stderr)) if verbose else (lambda m: None)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=[
            "--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = browser.new_context(
            locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
        )
        page = context.new_page()
        log(f"打开 {login_url}")
        page.goto(login_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        before_url, before_cookies = page.url, len(context.cookies())

        user_el, user_sel = (_first(page, [user_field]) if user_field else (None, None))
        if user_el is None:
            user_el, user_sel = _first(page, USER_FIELD_SELECTORS)
        pass_el, pass_sel = (_first(page, [pass_field]) if pass_field else (None, None))
        if pass_el is None:
            pass_el, pass_sel = _first(page, PASS_FIELD_SELECTORS)

        if user_el is None or pass_el is None:
            shot = Path(state_path).with_suffix(".png")
            page.screenshot(path=str(shot), full_page=True)
            browser.close()
            raise LoginError(f"未找到账号/密码输入框(截图: {shot})。"
                             f"用 --login-user-field / --login-pass-field 手动指定选择器")

        log(f"账号框 {user_sel} | 密码框 {pass_sel}")
        user_el.fill(user)
        pass_el.fill(password)

        submit_el, submit_sel = (_first(page, [submit_field]) if submit_field else (None, None))
        if submit_el is None:
            submit_el, submit_sel = _first(page, SUBMIT_SELECTORS)
        if submit_el is not None:
            log(f"提交 {submit_sel}")
            submit_el.click()
        else:
            log("未找到提交按钮,回车提交")
            pass_el.press("Enter")

        try:
            # 别拿整个 timeout(默认 30 秒)去等 networkidle —— 登录页普遍有轮询/心跳,
            # 这个状态可能永远不出现,白等满 30 秒。登录是否成功由下面的
            # detect_login_state 判定,不依赖这个等待,给个短预算就够。
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        if keep_open:
            log(f"保持浏览器打开 {keep_open}s,期间可人工处理验证码/扫码")
            page.wait_for_timeout(keep_open * 1000)

        html = page.content()
        ok, why = detect_login_state(html, before_url, page.url)
        after_cookies = context.cookies()
        if ok:
            log(f"登录成功: {why}")
            if len(after_cookies) > before_cookies:
                log(f"新增 cookie {len(after_cookies) - before_cookies} 个")
        else:
            log(f"登录判定失败: {why}(仍会保存状态,可能站点特征与判定规则不符)")

        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=state_path)
        payload = json.loads(Path(state_path).read_text(encoding="utf-8"))
        payload["_armory"] = {"login_url": login_url, "saved_at": time.time(),
                              "ok": ok, "reason": why, "cookies": len(after_cookies)}
        # 原子写 + 0600:存档里是会话凭证
        import sys as _sys
        from pathlib import Path as _P
        _sys.path.insert(0, str(_P(__file__).resolve().parent))
        from handoff import write_secret
        write_secret(state_path, json.dumps(payload, ensure_ascii=False, indent=2))
        log(f"状态已保存: {state_path}({len(after_cookies)} 个 cookie)")
        browser.close()
    return {"ok": ok, "reason": why, "cookies": len(after_cookies), "path": str(state_path)}


def load_state(path):
    """读状态文件:返回 (cookies 字典, 元信息)。

    cookie 要同时认两种格式 —— handoff 存的是 `{name: value}` 字典,
    playwright 导出的 storage_state 是 `[{"name":…, "value":…}]` 列表。
    只认列表会对着字典抛 AttributeError。`handoff.load_domain_state` 早先
    已经踩过这个坑,这里曾经漏掉过。
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = payload.get("cookies") or {}
    cookies = {}
    if isinstance(raw, dict):
        cookies = {str(k): str(v) for k, v in raw.items()}
    else:
        for c in raw:
            if isinstance(c, dict) and c.get("name"):
                cookies[c["name"]] = c.get("value", "")
    meta = payload.get("_armory") or {}
    if meta.get("saved_at"):
        meta["age_hours"] = round((time.time() - meta["saved_at"]) / 3600, 1)
    return cookies, meta


def describe_state(path):
    """给人看的登录态摘要。"""
    cookies, meta = load_state(path)
    lines = [f"状态文件: {path}"]
    if meta:
        age = meta.get("age_hours")
        lines.append(f"登录于: {meta.get('login_url', '?')}"
                     + (f"(距今 {age} 小时)" if age is not None else ""))
        lines.append(f"判定: {'成功' if meta.get('ok') else '未确认'} — {meta.get('reason', '')}")
    lines.append(f"Cookie {len(cookies)} 项: " + ", ".join(list(cookies)[:12]))
    if meta.get("age_hours") is not None and meta["age_hours"] > 24:
        lines.append("提示: 已超过 24 小时,很多站点的会话会失效,考虑重新登录")
    return "\n".join(lines)


# ---------------------------------------------------------------- 续期

def check_session(state_path, check_url, timeout=20, cookies=None):
    """用一个需要登录的 URL 探活。返回 (是否有效/None 未知, 说明)。

    判定优先级:重定向到登录页 > 页面出现登出特征 > 页面提示需登录。
    很多站点在会话失效时是「静默降级」——不报错、不跳转,只是把你当游客,
    所以光看状态码没用,得看页面里有没有登录态特征。
    """
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent))
    import engine as eng

    if cookies is None:
        cookies, _ = load_state(state_path)
    e = eng.Engine(rate=0, timeout=timeout, retries=0, cookies=cookies,
                   prefer_cffi=True, keepalive=False)
    res = e.fetch_static(check_url)
    if res.error:
        return None, f"探活请求失败: {res.error[:70]}"

    final = (res.final_url or "").lower()
    if any(k in final for k in ("login", "signin", "sign-in", "passport", "auth.")):
        return False, f"被重定向到登录页 ({res.final_url[:70]})"

    low = (res.html or "").lower()
    if any(h in low for h in LOGGED_IN_HINTS):
        return True, "页面出现登出/账户特征"
    # 未登录的页面往往既没有登出特征,也没有「请登录」字样 —— 只有一个登录入口。
    # 不认这一点的话,会话失效会被判成「无法判定」,续期就触发不了。
    import re as _re
    if _re.search(r"""href=["'][^"']*(?:/login|/signin|/sign-in|login\?|passport|/auth)""", low):
        return False, "页面出现登录入口,且无登出特征"
    if any(h in low for h in ("请先登录", "登录后查看", "sign in to continue",
                              "please log in", "login required")):
        return False, "页面提示需要登录"
    return None, "无法判定(页面既无登录也无登出特征)"


# 需要登录才返回身份的信息端点。有表走表 —— JSON 接口的判定比 HTML 干净得多:
# 401 就是 401,不会像很多首页那样对匿名用户也返回 200 加满屏"登录"字样。
# 表里没有的站点走 probe_state() 的差分探测,不依赖这张表。
IDENTITY_ENDPOINTS = {
    "www.zhihu.com": "https://www.zhihu.com/api/v4/me",
    "zhihu.com": "https://www.zhihu.com/api/v4/me",
    "www.bilibili.com": "https://api.bilibili.com/x/web-interface/nav",
    "bilibili.com": "https://api.bilibili.com/x/web-interface/nav",
    "weibo.com": "https://weibo.com/ajax/profile/info",
    "www.weibo.com": "https://weibo.com/ajax/profile/info",
    "github.com": "https://api.github.com/user",
    "api.github.com": "https://api.github.com/user",
    "x.com": "https://api.x.com/1.1/account/verify_credentials.json",
    "twitter.com": "https://api.x.com/1.1/account/verify_credentials.json",
}


def identity_probe_url(url):
    """站点 → 身份接口。没有就返回 None,交给差分探测。"""
    from urllib.parse import urlparse
    host = (urlparse(url if "//" in url else "//" + url).hostname or "").lower()
    if host in IDENTITY_ENDPOINTS:
        return IDENTITY_ENDPOINTS[host]
    # 退一步认根域:mail.zhihu.com 也该认到 www.zhihu.com 的表。
    # 必须整段匹配 —— 曾经写成 known.split(".", 1)[-1] 取后缀,那样 "github.com"
    # 会退化成 "com",所有 .com 站点都会去打 GitHub 的 /user 并吃 401,
    # 被误报成「登录态已失效」而触发无谓重登。
    for known, endpoint in IDENTITY_ENDPOINTS.items():
        if host == known or host.endswith("." + known):
            return endpoint
    return None


def _identity_verdict(status, body):
    """身份接口响应 → (是否已登录, 说明)。None 表示判不出来。"""
    if status in (401, 403):
        return False, f"身份接口 {status}(凭证被拒)"
    if status != 200:
        return None, f"身份接口 {status},无法判定"
    try:
        data = json.loads(body)
    except Exception:
        return None, "身份接口响应不是 JSON"
    flat = json.dumps(data, ensure_ascii=False).lower().replace(" ", "")
    for key in ("islogin", "is_login", "logged_in", "loggedin", "isauthenticated"):
        if f'"{key}":false' in flat:
            return False, "身份接口明确标记未登录"
    for key in ("islogin", "is_login", "logged_in", "loggedin", "isauthenticated"):
        if f'"{key}":true' in flat:
            return True, "身份接口确认已登录"
    if isinstance(data, dict):
        if data.get("error") or data.get("error_code"):
            return False, "身份接口返回错误(通常表示凭证失效)"
        if any(k in data for k in ("name", "login", "screen_name", "userName",
                                   "uid", "id", "user", "data")):
            return True, "身份接口返回了身份字段"
    return None, "身份接口响应无法判定"


def probe_state(url, state_path=None, cookies=None, timeout=20):
    """主动探测「这个存档登录态现在还能不能用」。

    返回 (valid, reason, method),valid 为 None 表示判不出来。

    两条路:
      1. 站点在 IDENTITY_ENDPOINTS 表里 → 直接打身份接口,最准
      2. 不在表里 → **差分探测**:带登录态与匿名各请求一次,比响应差异。
         这一路是必要的 —— 大量站点对匿名和登录用户返回同一个 200 页面,
         只差头像和用户名,单看一次请求根本分不出登录态有没有生效。
    """
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent))
    import engine as eng

    if cookies is None:
        cookies, _ = load_state(state_path) if state_path else ({}, None)
    if not cookies:
        return None, "没有可用的 cookie", "none"

    e = eng.Engine(rate=0, timeout=timeout, retries=0, cookies=cookies,
                   prefer_cffi=True, keepalive=False)

    endpoint = identity_probe_url(url)
    if endpoint:
        res = e.fetch_static(endpoint)
        if res.error:
            return None, f"身份接口请求失败: {res.error[:70]}", "identity"
        valid, why = _identity_verdict(res.status, res.html or "")
        return valid, why, "identity"

    anon = eng.Engine(rate=0, timeout=timeout, retries=0, prefer_cffi=True, keepalive=False)
    a = anon.fetch_static(url)
    b = e.fetch_static(url)
    if b.error:
        return None, f"探活请求失败: {b.error[:70]}", "differential"
    if a.error:
        return None, f"匿名对照请求失败: {a.error[:70]}", "differential"

    def hint_hits(h):
        low = (h or "").lower()
        return sum(1 for k in LOGGED_IN_HINTS if k in low)

    auth_hits, anon_hits = hint_hits(b.html), hint_hits(a.html)
    if auth_hits > anon_hits:
        return True, f"带登录态出现 {auth_hits} 处登录特征,匿名仅 {anon_hits} 处", "differential"
    if anon_hits > auth_hits:
        return False, f"匿名反而多出登录特征(带 {auth_hits} / 匿 {anon_hits}),存档可疑", "differential"
    if auth_hits and anon_hits:
        return None, f"两侧都出现登录特征({auth_hits} 处),该页无法区分身份", "differential"

    # 没有登录/登出字样时,退到「实质内容量」比较:登录态通常能看到更多内容
    from handoff import visible_text_len
    la, lb = visible_text_len(a.html), visible_text_len(b.html)
    if lb > la * 1.15 and lb - la > 200:
        return True, f"带登录态正文 {lb} 字 vs 匿名 {la} 字,内容更多", "differential"
    if la and lb < la * 0.85:
        return False, f"带登录态正文反而更少({lb} vs {la} 字),可能被降级", "differential"
    return None, f"响应几乎一致(正文 {lb} vs {la} 字),分不出登录态是否生效", "differential"


def ensure_valid(state_path, check_url, login_url=None, user=None, password=None,
                 auto_relogin=True, verbose=True, **login_kwargs):
    """探活 + 失效时自动重登。

    登录态过期是长任务里必然发生的事 —— 与其让抓取在半夜静默失败,
    不如每次开跑前探一次,失效就自己续上。
    """
    ok, why = check_session(state_path, check_url)
    if ok is True:
        if verbose:
            print(f"[i] 登录态有效: {why}", file=sys.stderr)
        return {"valid": True, "relogged": False, "reason": why}

    if ok is None:
        if verbose:
            print(f"[i] 登录态无法判定: {why}", file=sys.stderr)
        return {"valid": None, "relogged": False, "reason": why}

    if not (auto_relogin and login_url and user and password):
        if verbose:
            print(f"[i] 登录态已失效: {why}(缺少 --login-* 凭据,无法自动续期)", file=sys.stderr)
        return {"valid": False, "relogged": False, "reason": why}

    if verbose:
        print(f"[i] 登录态已失效({why}),自动重新登录…", file=sys.stderr)
    login(login_url, user, password, state_path, verbose=verbose, **login_kwargs)
    ok2, why2 = check_session(state_path, check_url)
    if verbose:
        print(f"[i] 续期结果: {'成功' if ok2 else '仍未通过'} — {why2}", file=sys.stderr)
    return {"valid": ok2, "relogged": True, "reason": why2,
            "was": why}
