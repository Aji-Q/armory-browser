#!/usr/bin/env python3
"""scout —— 站点采集侦察器

拿到一个 URL，先用最少量的普通 HTTP 请求判明「这个站该怎么采、会撞上什么」，
给出对抗层级判定和可直接执行的下步策略。

判定维度取自 armory 素材里的知识体系
(inbox/CrawlerTutorial/docs/爬虫进价/02、05 章的反爬检测梯度):

    低  UA 检测、请求头完整性
    中  访问频率、Cookie 登录态、API 参数签名
    高  TLS 指纹、JS 环境检测、验证码

只做三件事:发请求 → 读响应特征 → 给结论。不绕过任何防护,不提交表单。

用法:
    python3 modules/scout/scout.py https://example.com
    python3 modules/scout/scout.py https://example.com --json
    python3 modules/scout/scout.py https://example.com --no-naive   # 只发基线请求
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
NAIVE_UA = "python-requests/2.31.0"

# 边缘节点 / WAF / 反爬厂商指纹。键是厂商,值是命中的头名或 Cookie 名(小写比对)
EDGE_SIGNATURES = {
    "Cloudflare": ["cf-ray", "cf-cache-status", "__cf_bm", "cf_clearance", "_cfuvid"],
    "Akamai": ["akamai-grn", "x-akamai-transformed", "akamai-origin-hop", "_abck", "bm_sz"],
    "AWS CloudFront": ["x-amz-cf-id", "x-amz-cf-pop", "cloudfront"],
    "阿里云 WAF": ["acw_tc", "acw_sc__v2", "aliyungf_tc", "aliyun_waf", "yundun"],
    "腾讯云 WAF": ["stgw", "tsec", "tencent_waf"],
    "Imperva/Incapsula": ["visid_incap", "incap_ses", "x-iinfo", "nlbi_"],
    "F5 BIG-IP": ["bigipserver", "ts01", "x-wa-info"],
    "Sucuri": ["x-sucuri-id", "x-sucuri-cache"],
    "Fastly": ["x-served-by", "x-fastly-request-id", "fastly-io-info"],
    "网宿 WAF": ["wzws", "ws-web", "wzws_sessionid"],
    "加速乐": ["__jsl_clearance", "__jsluid", "jsl_clearance"],
    "瑞数信息": ["riversafe", "rsa_", "rsf_"],
    "百度云加速": ["yunjiasu", "yunsuo_session"],
    "盾山/其他国产 WAF": ["hwwafsesid", "hwwafsednsid", "safedog", "waf_cookie"],
}

RATE_LIMIT_HEADERS = [
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "retry-after", "x-rate-limit", "ratelimit-limit",
]

# 验证码类型。按厂商特征串,顺序即优先级(越靠前越难缠)
CAPTCHA_SIGNATURES = {
    "Cloudflare Turnstile": ["challenges.cloudflare.com", "cf-turnstile", "turnstile.render"],
    "reCAPTCHA": ["google.com/recaptcha", "g-recaptcha", "grecaptcha", "recaptcha/api.js"],
    "hCaptcha": ["hcaptcha.com", "h-captcha"],
    "极验 Geetest": ["geetest", "gt.js", "initgeetest", "gt_c.js"],
    "腾讯验证码": ["captcha.gtimg.com", "tcaptcha", "tencent-captcha"],
    "阿里滑块": ["punish", "aliyun-captcha", "nc_1_n1z", "nocaptcha"],
    "滑块验证": ["slidercaptcha", "slider-verify", "drag-verify", "verify-slide"],
    "图形验证码": ["captcha", "verifycode", "checkcode", "vcode", "imgcode"],
}

# JS 挑战 / 反爬脚本特征
JS_CHALLENGE_SIGNATURES = {
    "JS Cookie 计算挑战": ["__jsl_clearance", "acw_sc__v2", "document.cookie=(", "location.href=location.pathname"],
    "浏览器指纹采集": ["toDataURL", "getImageData", "webgl", "navigator.webdriver", "fingerprint2", "fingerprintjs"],
    "代码混淆": ["eval(function(p,a,c,k,e", "_0x", "\\x68\\x65\\x61\\x64"],
    "静默重定向": ["setTimeout(function(){window.location", "top.location.href="],
}

# 前端渲染特征:命中说明首屏 HTML 不含目标数据
SPA_SIGNATURES = {
    "React 挂载点": ['<div id="root"', 'data-reactroot'],
    "Vue 挂载点": ['<div id="app"', 'data-v-app', 'v-cloak'],
    "Angular 挂载点": ["ng-app", "ng-version", "_nghost"],
    "Next.js": ["__NEXT_DATA__"],
    "Nuxt": ["window.__NUXT__"],
    "Svelte/SvelteKit": ["__sveltekit", "svelte-"],
}

# 内嵌数据:命中就不需要渲染,直接解析 JSON 更快
EMBEDDED_DATA = {
    "__NEXT_DATA__": r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    "window.__NUXT__": r"window\.__NUXT__\s*=\s*(\{.*?\});",
    "window.__INITIAL_STATE__": r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});",
    "application/ld+json": r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    "window.__DATA__": r"window\.__DATA__\s*=\s*(\{.*?\});",
}

# 异步数据加载。首屏 HTML 有内容、目标数据却由 XHR 取回 ——
# 页面级的文本比与脚本比都抓不到这类页,得靠「空容器 + 异步调用」的组合。
AJAX_SIGNATURES = {
    "异步调用": (r"(?:\$\.(?:ajax|get|post|getJSON)\s*\(|fetch\s*\(\s*[\"'`]"
              r"|XMLHttpRequest\s*\(|axios\.(?:get|post|request)\s*\()"),
    "空数据容器": (r"(?:<tbody[^>]*>\s*</tbody>"
               r"|<(?:ul|ol|table|div)[^>]+(?:id|class)=\"[^\"]*\"[^>]*>\s*</(?:ul|ol|table|div)>)"),
    "加载占位": r"(?:Loading\.\.\.|加载中|正在加载|loading-spinner|skeleton-)",
}

# 陷阱:给爬虫准备的,真人看不见。只有高置信这档计入风险
HONEYPOT_SIGNATURES = {
    "诱饵表单字段": r'<input[^>]+type="hidden"[^>]+name="(?:honeypot|trap|url|website|email_confirm|nickname_check)"',
    "负偏移定位": r'style="[^"]*(?:left\s*:\s*-\d{3,}|top\s*:\s*-\d{3,}|text-indent\s*:\s*-\d{3,})',
    "零尺寸诱饵链接": r'style="[^"]*(?:opacity\s*:\s*0|width\s*:\s*0[^.\d]|height\s*:\s*0[^.\d])[^"]*"[^>]*>\s*<a\b',
    "同名诱饵字段": r'<input[^>]+class="[^"]*(?:hidden|trap|bot)[^"]*"',
}

# 低置信:响应式设计里 display:none 的链接是常规做法,单独列出仅供参考,不当陷阱
HIDDEN_LINK_SIGNATURE = r'<a[^>]+style="[^"]*(?:display\s*:\s*none|visibility\s*:\s*hidden)[^"]*"[^>]*>'

VERDICT_LEVELS = ["静态直取", "内嵌数据直取", "动态渲染", "反检测渲染", "验证码闸门"]


@dataclass
class Response:
    url: str
    status: int = 0
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    elapsed: float = 0.0
    final_url: str = ""
    error: str = ""

    @property
    def text(self):
        try:
            return self.body.decode("utf-8", errors="replace")
        except Exception:
            return ""


def fetch(url, headers=None, timeout=12, max_bytes=3_000_000):
    """单次普通 GET。不发 Accept-Encoding,避免拿回压缩体难以判长度。"""
    hdrs = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    res = Response(url=url)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            res.status = resp.status
            res.headers = {k.lower(): v for k, v in resp.headers.items()}
            res.final_url = resp.url
            body = resp.read(max_bytes)
            enc = (res.headers.get("content-encoding") or "").lower()
            # 解压失败要单独说明,不能让它冒充网络错误、也不能冒泡。
            # gzip.BadGzipFile 是 OSError 子类,会被下面的 except 吞成"网络故障";
            # zlib.error 不是,会直接抛出把 scout 打死。两者都不该表现为
            # "这站点返回了乱码" —— 保留原始 body 但明确记下真正的原因。
            try:
                if "gzip" in enc:
                    body = gzip.decompress(body)
                elif "deflate" in enc:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            except Exception as exc:
                res.error = f"解压失败({enc}): {type(exc).__name__}: {str(exc)[:80]}"
            res.body = body
    except urllib.error.HTTPError as exc:
        res.status = exc.code
        res.headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        res.final_url = exc.url or url
        try:
            res.body = exc.read(max_bytes)
        except Exception:
            pass
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as exc:
        res.error = f"{type(exc).__name__}: {exc}"
    res.elapsed = time.time() - started
    return res


# ---------------------------------------------------------------- 特征提取

def header_text(value):
    """响应头值可能是 list(curl_cffi 下多个同名头会聚成列表),统一成串。"""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value or "")


def match_edges(headers):
    """响应头 + Cookie 名里比对边缘节点/反爬厂商。"""
    blob = " ".join(f"{k}={header_text(v)}" for k, v in headers.items())
    set_cookie = header_text(headers.get("set-cookie", "")).lower()
    found = {}
    for vendor, keys in EDGE_SIGNATURES.items():
        hits = [k for k in keys if k in blob or k in set_cookie]
        if hits:
            found[vendor] = hits
    if re.search(r"^cloudflare$", header_text(headers.get("server", "")), re.I):
        found.setdefault("Cloudflare", []).append("server: cloudflare")
    return found


def match_table(text, table, regex=False):
    """在文本里比对特征表,返回 {标签: 首个命中片段}。"""
    hits = {}
    low = text.lower()
    for label, keys in table.items():
        for key in keys:
            if (re.search(key, text) if regex else key.lower() in low):
                hits[label] = key
                break
    return hits


def page_shape(html):
    """返回 (可见文本比, 脚本体积比)。

    只看文本比会误判:标签膨胀的静态页(books.toscrape 51KB 里只有 1.8KB 文本)
    和 JS 注入的页面文本比一样低。脚本占比才能把两者分开 ——
    JS 渲染页的 script 能占到七成。
    """
    if not html:
        return 0.0, 0.0
    total = len(html)
    no_script = re.sub(
        r"(?is)<(?:script|style|noscript|template)[^>]*>.*?</(?:script|style|noscript|template)>",
        " ", html)
    text = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", no_script)).strip()
    return len(text) / max(total, 1), 1 - len(no_script) / max(total, 1)


def extract_embedded(html):
    """尝试解析内嵌 JSON,判断数据是否已在首屏 HTML 里。"""
    out = {}
    for label, pattern in EMBEDDED_DATA.items():
        m = re.search(pattern, html, re.S)
        if not m:
            continue
        raw = m.group(1).strip()
        size = len(raw)
        parsed = None
        try:
            parsed = json.loads(raw)
        except Exception:
            pass
        out[label] = {"bytes": size, "valid_json": parsed is not None,
                      "top_keys": sorted(parsed.keys())[:12] if isinstance(parsed, dict) else None}
    return out


def find_api_hints(html):
    """页面里暴露的接口线索 —— 直连接口往往比渲染页面省事一个数量级。"""
    hints = set()
    for m in re.finditer(r"""["'](/(?:api|ajax|graphql|rest|v\d)/[^"'\s]{0,80})["']""", html):
        hints.add(m.group(1))
    for m in re.finditer(r"""["'](https?://[^"'\s]{0,60}/(?:api|graphql)[^"'\s]{0,60})["']""", html):
        hints.add(m.group(1))
    return sorted(hints)[:12]


def score_request_layer(baseline, naive):
    """UA/Header 校验强度:拿裸 UA 的响应和基线比。"""
    if naive is None:
        return "未测", []
    if naive.error and not baseline.error:
        return "高", [f"裸 UA 直接失败: {naive.error}"]
    if naive.status in (401, 403, 405, 406, 412, 429) and baseline.status < 400:
        return "高", [f"裸 UA 被拒 {naive.status},浏览器 UA 正常 {baseline.status}"]
    if naive.status >= 500 and baseline.status < 400:
        return "中", [f"裸 UA 触发 {naive.status},疑似按 UA 区分处理"]
    if naive.status == baseline.status and abs(len(naive.body) - len(baseline.body)) < 64:
        return "低", ["裸 UA 与浏览器 UA 响应一致"]
    return "中", [f"响应有差异: 裸 UA {naive.status}/{len(naive.body)}B, "
                  f"浏览器 UA {baseline.status}/{len(baseline.body)}B"]


# 藏在 URL 或请求参数里的签名。很多接口不混淆 JS,只是要求带一个 sign 参数 ——
# 只盯「混淆脚本」会漏掉这一大类
SIGN_IN_URL_RE = re.compile(
    r"[?&\"'](sign|signature|_sign|x-sign|sign_key|token|_token|nonce|access_key|"
    r"x-zse-\d+|x-s|x-t|timestamp|ts)[\"']?\s*[=:]\s*[\"']?[^&\s\"']{0,80}", re.I)


def find_sign_params(html):
    """页面/URL 里出现的签名类参数名。"""
    return sorted({m.group(1).lower() for m in SIGN_IN_URL_RE.finditer(html)})


def build_evidence(html, headers, request_level="低", request_evidence=None,
                   login_evidence=None):
    """从一份响应里提取全部判定证据。

    scout 与 harvest 共用这条判定链 —— 侦察与执行看到的必须是同一套结论。
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    ev = {
        "edges": match_edges(headers),
        "captcha": match_table(html, CAPTCHA_SIGNATURES),
        "js_challenge": match_table(html, JS_CHALLENGE_SIGNATURES),
        "spa": match_table(html, SPA_SIGNATURES),
        "honeypot": {k: True for k, pat in HONEYPOT_SIGNATURES.items()
                     if re.search(pat, html, re.I)},
        "hidden_links": len(re.findall(HIDDEN_LINK_SIGNATURE, html, re.I)),
        "ajax": {k: bool(re.search(pat, html, re.I)) for k, pat in AJAX_SIGNATURES.items()},
        "rate_headers": {h: headers[h] for h in RATE_LIMIT_HEADERS if h in headers},
        "embedded": extract_embedded(html),
        "api_hints": find_api_hints(html),
        "sign_params": find_sign_params(html),
        "request_level": request_level,
        "request_evidence": request_evidence or [],
        "login_required": bool(login_evidence),
        "login_evidence": login_evidence or [],
    }
    ev["text_ratio"], ev["script_ratio"] = page_shape(html)
    ev["html_bytes"] = len(html)
    ev["cdn_challenge"] = [f"{c} 已下发" for c in
                           ("cf_clearance", "__jsl_clearance", "acw_sc__v2", "rsf_")
                           if c in header_text(headers.get("set-cookie", "")).lower()]
    if "challenges.cloudflare.com" in html.lower():
        ev["cdn_challenge"].append("challenges.cloudflare.com 脚本")
    return ev


def decide(ev):
    """把证据收敛成一个可执行的策略结论。"""
    layers = {}
    reasons = []

    # 请求特征层
    layers["请求特征层"] = {"level": ev["request_level"], "evidence": ev["request_evidence"]}

    # 行为特征层
    if ev["rate_headers"]:
        layers["行为特征层"] = {"level": "有显式限流头",
                            "evidence": [f"{k}: {v}" for k, v in ev["rate_headers"].items()]}
    else:
        layers["行为特征层"] = {"level": "未检出",
                            "evidence": ["无限流响应头;真实阈值需多请求采样"]}

    # 会话层
    if ev["login_required"]:
        layers["会话层"] = {"level": "需登录态", "evidence": ev["login_evidence"]}
    else:
        layers["会话层"] = {"level": "匿名可取", "evidence": ["未见登录跳转或 401"]}

    # 签名层:有接口线索,且(有混淆/挑战脚本 或 参数里出现签名名)。
    # 后者覆盖「JS 不混淆但接口要签名」的大类,只认前者会漏掉太多
    sign_params = ev.get("sign_params") or []
    if ev["api_hints"] and (ev["js_challenge"] or sign_params):
        why = []
        if ev["js_challenge"]:
            why.append("页面含混淆/挑战脚本")
        if sign_params:
            why.append("参数含 " + ", ".join(sign_params[:4]))
        layers["签名层"] = {"level": "疑似参数签名", "evidence": why}
    else:
        layers["签名层"] = {"level": "未检出", "evidence": ["未见签名参数模式"]}

    # 指纹层:只看挑战特征。挂在 CDN 后面是基础设施选择,不等于会被指纹检测
    if ev["captcha"]:
        layers["指纹层"] = {"level": "高", "evidence": ["存在验证码,指纹检测概率高"]}
    elif ev["js_challenge"]:
        layers["指纹层"] = {"level": "高",
                          "evidence": [f"{k}: {v}" for k, v in ev["js_challenge"].items()]}
    elif ev["cdn_challenge"]:
        layers["指纹层"] = {"level": "高", "evidence": ev["cdn_challenge"]}
    elif ev["spa"]:
        layers["指纹层"] = {"level": "中", "evidence": ["前端框架渲染,可能存在客户端检测"]}
    else:
        layers["指纹层"] = {"level": "低", "evidence": ["未见 JS 挑战或指纹脚本"]}

    # 验证码层
    layers["验证码层"] = ({"level": "有", "evidence": [f"{k} ({v})" for k, v in ev["captcha"].items()]}
                       if ev["captcha"]
                       else {"level": "无", "evidence": ["未命中验证码特征"]})

    # 策略收敛:按优先级,验证码与 JS 挑战是硬门槛
    if ev["captcha"]:
        verdict, why = "验证码闸门", f"命中验证码: {', '.join(ev['captcha'])}"
    elif ev["js_challenge"]:
        verdict, why = "反检测渲染", f"命中 JS 挑战/指纹脚本: {', '.join(ev['js_challenge'])}"
    elif ev["embedded"] and any(v["valid_json"] for v in ev["embedded"].values()):
        keys = [k for k, v in ev["embedded"].items() if v["valid_json"]]
        verdict, why = "内嵌数据直取", f"首屏内含可解析 JSON: {', '.join(keys)}"
    elif ev["spa"] and ev["text_ratio"] < 0.05:
        verdict, why = "动态渲染", "首屏命中前端框架挂载点: " + ", ".join(ev["spa"])
    elif ev["script_ratio"] >= 0.5 and ev["text_ratio"] < 0.05:
        verdict, why = "动态渲染", (f"脚本占文档 {ev['script_ratio']:.0%}、可见文本仅 "
                                 f"{ev['text_ratio']:.1%} —— 数据由 JS 注入")
    elif ev["ajax"].get("异步调用") and ev["ajax"].get("空数据容器"):
        verdict, why = "动态渲染", "空数据容器 + 异步调用并存 —— 目标数据由 XHR 注入,渲染或直连接口"
    elif ev["text_ratio"] < 0.015:
        verdict, why = "动态渲染", f"可见文本占比仅 {ev['text_ratio']:.1%},数据大概率由 JS 注入"
    else:
        verdict, why = "静态直取", (f"可见文本 {ev['text_ratio']:.1%}、脚本占比 "
                                 f"{ev['script_ratio']:.0%},数据在首屏 HTML 里")

    if ev["edges"]:
        reasons.append("边缘节点: " + ", ".join(f"{k}({','.join(v)})" for k, v in ev["edges"].items()))
    if ev["rate_headers"]:
        reasons.append("限流头: " + ", ".join(ev["rate_headers"]))
    return verdict, why, layers, reasons


def plan(verdict, ev):
    """把结论映射到 armory 素材里的具体能力。"""
    if verdict == "静态直取":
        return ["httpx/requests + 浏览器 UA 与完整头(参见教程 02 章 headers_builder)",
                "速率压到 1-2 rps,不要并发开满",
                "解析用 lxml/parsel,别上浏览器"]
    if verdict == "内嵌数据直取":
        keys = [k for k, v in ev["embedded"].items() if v["valid_json"]]
        return [f"直接解析页面内 {', '.join(keys)},拿 JSON 走数据管道",
                "省掉渲染,等效于直连接口"]
    if verdict == "动态渲染":
        return ["Playwright 无头渲染(chromium),等 networkidle 后取 DOM",
                "或 crawl4ai 的 AsyncWebCrawler —— 自带适配与 markdown 提取",
                "若只需关键字段,先从接口线索直连,渲染是最后手段"]
    if verdict == "反检测渲染":
        return ["Scrapling 的 StealthyFetcher / Playwright + stealth.js(教程 05 章)",
                "检查 navigator.webdriver、window.chrome、plugins 三处基础特征",
                "指纹层的 Canvas/WebGL 差异需真实浏览器环境,沙箱里跑会露馅"]
    if verdict == "验证码闸门":
        return ["先判类型: 图形 → OCR;滑块 → 轨迹模拟(教程 08 章)",
                "reCAPTCHA/Turnstile 类基本劝退自动化,走人工或打码平台",
                "有验证码时务必配代理池,单 IP 反复试会连带封禁"]
    return []


def recon(url, do_naive=True, timeout=12):
    baseline = fetch(url, timeout=timeout)
    if baseline.error:
        return {"url": url, "fatal": baseline.error}

    naive = fetch(url, headers={"User-Agent": NAIVE_UA}, timeout=timeout) if do_naive else None
    html = baseline.text
    headers = baseline.headers

    request_level, request_evidence = score_request_layer(baseline, naive)

    login_evidence = []
    if baseline.status in (401, 407):
        login_evidence.append(f"状态码 {baseline.status}")
    if baseline.final_url and baseline.final_url != url and re.search(
            r"(login|signin|auth|passport|sso)", baseline.final_url, re.I):
        login_evidence.append(f"重定向到 {baseline.final_url}")

    ev = build_evidence(html, headers, request_level, request_evidence, login_evidence)
    verdict, why, layers, reasons = decide(ev)
    return {
        "url": url, "baseline": baseline, "naive": naive, "evidence": ev,
        "verdict": verdict, "why": why, "layers": layers, "reasons": reasons,
        "steps": plan(verdict, ev),
    }


# ---------------------------------------------------------------- 渲染

C_DIM, C_OK, C_WARN, C_BAD, C_KEY, C_OFF = "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[0m"


def render(res):
    if res.get("fatal"):
        return f"{C_BAD}目标不可达{C_OFF} {res['url']}\n  {res['fatal']}"
    b, ev = res["baseline"], res["evidence"]
    L = []
    A = L.append
    A(f"{C_KEY}目标{C_OFF}  {res['url']}")
    A(f"{C_DIM}      →{C_OFF} {b.final_url}" if b.final_url != res["url"] else "")
    A(f"{C_KEY}基线{C_OFF}  {b.status} | {b.elapsed:.2f}s | {len(b.body)/1024:.1f} KB | "
      f"{b.headers.get('content-type', '?')[:40]}")
    if ev["edges"]:
        A(f"{C_KEY}边缘{C_OFF}  " + ", ".join(f"{k}{C_DIM}[{','.join(v)}]{C_OFF}"
                                            for k, v in ev["edges"].items()))
    A("")
    A(f"{C_KEY}对抗层级{C_OFF}")
    order = ["请求特征层", "行为特征层", "会话层", "签名层", "指纹层", "验证码层"]
    for name in order:
        info = res["layers"][name]
        lvl = info["level"]
        color = C_BAD if lvl in ("高", "有", "需登录态", "疑似参数签名", "有显式限流头") else \
            (C_WARN if lvl == "中" else C_OK if lvl in ("低", "无", "匿名可取") else C_DIM)
        A(f"  {name:<10} {color}{lvl:<12}{C_OFF} {C_DIM}{'; '.join(info['evidence'])[:90]}{C_OFF}")
    A("")

    bits = []
    if ev["text_ratio"]:
        bits.append(f"可见文本 {ev['text_ratio']:.1%} / 脚本 {ev['script_ratio']:.0%}")
    if ev["spa"]:
        bits.append("框架 " + "/".join(ev["spa"]))
    if ev["js_challenge"]:
        bits.append("JS挑战 " + "/".join(ev["js_challenge"]))
    if ev.get("cdn_challenge"):
        bits.append("CDN挑战 " + ", ".join(ev["cdn_challenge"]))
    if ev["captcha"]:
        bits.append("验证码 " + "/".join(ev["captcha"]))
    if ev["embedded"]:
        bits.append("内嵌JSON " + ", ".join(f"{k}({v['bytes']}B"
                                        f"{',可解析' if v['valid_json'] else ',解析失败'})"
                                        for k, v in ev["embedded"].items()))
    if ev["honeypot"]:
        bits.append(f"{C_WARN}陷阱 {len(ev['honeypot'])} 处{C_OFF}")
    if ev.get("hidden_links"):
        bits.append(f"{C_DIM}隐藏链接 {ev['hidden_links']}(响应式设计的常规做法){C_OFF}")
    if ev["ajax"].get("异步调用") and ev["ajax"].get("空数据容器"):
        bits.append(f"{C_WARN}异步加载{C_OFF}(空容器+" + "异步调用)")
    if rate_headers_ := ev["rate_headers"]:
        bits.append("限流头 " + ", ".join(rate_headers_))
    if bits:
        A(f"{C_KEY}页面特征{C_OFF}  " + " | ".join(bits))
    if ev["api_hints"]:
        A(f"{C_KEY}接口线索{C_OFF}  " + ", ".join(ev["api_hints"][:6]))
    A("")

    vcolor = {"静态直取": C_OK, "内嵌数据直取": C_OK, "动态渲染": C_WARN,
              "反检测渲染": C_BAD, "验证码闸门": C_BAD}.get(res["verdict"], C_OFF)
    A(f"{C_KEY}判定{C_OFF}  {vcolor}{res['verdict']}{C_OFF}  {C_DIM}— {res['why']}{C_OFF}")
    A(f"{C_KEY}下步{C_OFF}")
    for s in res["steps"]:
        A(f"  · {s}")
    return "\n".join(x for x in L if x is not None)


def main():
    ap = argparse.ArgumentParser(description="站点采集侦察器 —— 判明该怎么采、会撞上什么")
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    ap.add_argument("--no-naive", action="store_true", help="跳过裸 UA 对照请求")
    ap.add_argument("--timeout", type=int, default=12)
    args = ap.parse_args()

    url = args.url if "://" in args.url else "https://" + args.url
    res = recon(url, do_naive=not args.no_naive, timeout=args.timeout)

    if args.json:
        if res.get("fatal"):
            print(json.dumps({"url": url, "error": res["fatal"]}, ensure_ascii=False))
            return 1
        b = res["baseline"]
        print(json.dumps({
            "url": res["url"], "status": b.status, "final_url": b.final_url,
            "elapsed": round(b.elapsed, 3), "bytes": len(b.body),
            "server": b.headers.get("server", ""),
            "edges": res["evidence"]["edges"],
            "verdict": res["verdict"], "why": res["why"],
            "layers": res["layers"],
            "text_ratio": round(res["evidence"]["text_ratio"], 4),
            "script_ratio": round(res["evidence"]["script_ratio"], 4),
            "cdn_challenge": res["evidence"]["cdn_challenge"],
            "spa": res["evidence"]["spa"], "js_challenge": res["evidence"]["js_challenge"],
            "captcha": res["evidence"]["captcha"], "embedded": res["evidence"]["embedded"],
            "api_hints": res["evidence"]["api_hints"], "honeypot": res["evidence"]["honeypot"],
            "rate_headers": res["evidence"]["rate_headers"],
            "steps": res["steps"],
        }, ensure_ascii=False, indent=2))
        return 0

    out = render(res)
    if sys.stdout.isatty():
        print(out)
    else:
        print(re.sub(r"\033\[[0-9;]*m", "", out))
    return 0 if not res.get("fatal") else 1


if __name__ == "__main__":
    sys.exit(main())
