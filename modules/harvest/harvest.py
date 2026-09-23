#!/usr/bin/env python3
"""harvest —— 爬取执行器

scout 负责判,harvest 负责收。后端选择由 scout 的侦察结论驱动,
不做无脑轮询。四个后端按成本递进:静态 → 内嵌数据 → 渲染 → 反检测。

用法:
    python3 modules/harvest/harvest.py https://example.com
    python3 modules/harvest/harvest.py example.com --json
    python3 modules/harvest/harvest.py example.com --css "h1, .price" --regex "\\d+ 元"
    python3 modules/harvest/harvest.py urls.txt -o out/ --concurrency 4
    python3 modules/harvest/harvest.py example.com --crawl 20 -o out/
    python3 modules/harvest/harvest.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scout"))

import auth as auth_mod     # noqa: E402
import engine as eng          # noqa: E402
import handoff as ho          # noqa: E402
import extract as ex          # noqa: E402
import scout as scout_mod     # noqa: E402


def normalize(url):
    if "://" not in url:
        url = "https://" + url
    return url


def slugify(url):
    parts = urlparse(url)
    path = re.sub(r"[^\w.\-]+", "_", parts.path.strip("/")) or "index"
    return f"{parts.netloc}__{path}"[:150]


def same_host(a, b):
    return urlparse(a).netloc == urlparse(b).netloc


def build_record(res, args, extra=None, keep_links=None):
    """把一次抓取收敛成一条结构化记录。keep_links 覆盖 args.links —— 深爬需要
    链接来入队,但用户没要链接时不该把 links 塞进最终记录。"""
    rec = {
        "url": res.url,
        "final_url": res.final_url,
        "status": res.status,
        "backend": res.backend,
        "verdict": res.verdict,
        "elapsed": round(res.elapsed, 3),
        "bytes": res.bytes,
        "retries": res.retries,
    }
    # 仅等待人工链路附带这些字段;超时降级不冒充完整抓取成功。
    for field in ("degraded", "human_status", "human_reason"):
        if hasattr(res, field):
            rec[field] = getattr(res, field)
    if res.error:
        rec["error"] = res.error
    if res.notes:
        rec["notes"] = res.notes
    if res.ua:
        rec["ua"] = res.ua            # 轮换模式下,出问题时得知道这次用的是哪个
    if res.proxy:
        rec["proxy"] = res.proxy
    if not res.html:
        if extra:
            rec.update(extra)
        return rec

    rec["meta"] = ex.extract_meta(res.html)
    text = ex.html_to_text(res.html)
    # 内容指纹,供深爬去重:同一篇文章的多个入口(带参、短链、翻页)只该抓一次。
    # 用去标签后的正文算 —— 算 URL 就没有意义了,那本来就是不同的。
    # 下划线开头是内部字段,save() 落盘前会摘掉。
    rec["_fp"] = hashlib.sha1(
        re.sub(r"\s+", " ", text).strip().encode("utf-8", "ignore")).hexdigest()

    if args.css:
        picked = [n.text() for n in ex.MiniDOM(res.html).select(args.css)]
        picked = [p for p in picked if p]
        rec["css"] = {"selector": args.css, "matches": len(picked), "values": picked[:200]}
    if args.regex:
        target = text if args.regex_on_text else res.html
        found = ex.apply_regex(target, args.regex)
        rec["regex"] = {"pattern": args.regex, "matches": len(found), "values": found[:200]}
    if (args.links if keep_links is None else keep_links):
        rec["links"] = ex.extract_links(res.html, res.final_url or res.url)
    if args.tables:
        rec["tables"] = ex.extract_tables(res.html)

    # 正文只在需要时转换并截断 —— 大页面的 markdown 会让记录膨胀一个数量级
    if args.format in ("markdown", "json"):
        md = ex.html_to_markdown(res.html)
        rec["markdown"] = (md or "")[:args.max_chars]
    if args.format == "text":
        rec["text"] = text[:args.max_chars]

    dom = ex.MiniDOM(res.html)
    rec["stats"] = {"text_chars": len(text), "links": len(dom.select("a")),
                    "images": len(dom.select("img"))}
    if extra:
        rec.update(extra)
    return rec


def render_human(rec, args):
    """给人看的一页摘要。"""
    if args.format == "json":
        return json.dumps(rec, ensure_ascii=False, indent=2)

    L = []
    A = L.append
    A(f"# {rec.get('meta', {}).get('title') or rec['url']}")
    A("")
    A(f"- URL: {rec['url']}")
    if rec.get("final_url") and rec["final_url"] != rec["url"]:
        A(f"- 重定向: {rec['final_url']}")
    A(f"- 状态: {rec['status']} | 后端: **{rec['backend']}** | {rec['elapsed']}s | "
      f"{rec['bytes'] / 1024:.1f} KB")
    if rec.get("verdict"):
        A(f"- 侦察判定: {rec['verdict']}")
    if rec.get("error"):
        A(f"- **错误**: {rec['error']}")
    for note in rec.get("notes", []):
        A(f"- 备注: {note}")
    if rec.get("css"):
        A("")
        A(f"## CSS `{rec['css']['selector']}` — {rec['css']['matches']} 处")
        for v in rec["css"]["values"][:20]:
            A(f"- {v[:200]}")
    if rec.get("regex"):
        A("")
        A(f"## 正则 `{rec['regex']['pattern']}` — {rec['regex']['matches']} 处")
        for v in rec["regex"]["values"][:20]:
            A(f"- {v[:200]}")
    body = rec.get("markdown") if args.format == "markdown" else rec.get("text")
    if body:
        A("")
        A("---")
        A("")
        A(body)
    return "\n".join(L)


def record_ok(rec):
    """CLI/队列共用传输成功判据;无状态或未完成重定向不能算成功。"""
    status = rec.get("status", 0)
    return (not rec.get("error") and not rec.get("degraded")
            and isinstance(status, int) and 200 <= status < 300)


def worker(args, urls, engine):
    """并发抓一批 URL。"""
    results = []
    # 人工接管会打开共享浏览器/更新会话,同一批只能等待一位操作者。
    # 普通自动模式仍保留用户选择的并发度。
    workers = 1 if args.wait_human is not None else max(args.concurrency, 1)

    def failed(url, exc):
        # 与 build_record 的基础结构一致,少量结果的 render_human 也能安全展示。
        return {"url": url, "final_url": "", "status": 0, "backend": args.backend,
                "verdict": "", "elapsed": 0.0, "bytes": 0, "retries": 0,
                "error": f"{type(exc).__name__}: {exc}"}

    def one(url, keep_links=None):
        try:
            if args.wait_human is not None:
                res = fetch_with_wait_human(engine, url, args, scout_mod, args.wait_human)
            else:
                res = engine.fetch(url, backend=args.backend, scout_mod=scout_mod)
            return build_record(res, args, keep_links=keep_links)
        except Exception as exc:
            return failed(url, exc)

    if args.crawl:
        start = urls[0]
        seen, frontier, seen_fp = {start}, [start], set()
        # 结果预算与抓取次数分开:重复页仍可能引出新链接,但不能无限追逐变体。
        max_attempts = getattr(args, "max_crawl_attempts", None) or max(args.crawl * 10, 1)
        attempts = 0
        while frontier and len(results) < args.crawl and attempts < max_attempts:
            size = min(args.crawl - len(results), max_attempts - attempts)
            batch, frontier = frontier[:size], frontier[size:]
            attempts += len(batch)
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers) as pool:
                recs = list(pool.map(lambda u: one(u, keep_links=True), batch))
            for rec in recs:
                # 链接发现不等于正文收录:重复正文页面的独有链接仍需入队。
                links = rec.get("links", [])
                if record_ok(rec):
                    for link in links:
                        href = link["href"]
                        if href in seen:
                            continue
                        if args.crawl_same_host and not same_host(href, start):
                            continue
                        seen.add(href)
                        frontier.append(href)
                if not args.links:
                    rec.pop("links", None)
                fp = rec.get("_fp") if record_ok(rec) else None
                if fp and fp in seen_fp:
                    print(f"  [重复内容,跳过] {rec.get('url')}", file=sys.stderr)
                    continue
                if fp:
                    seen_fp.add(fp)
                results.append(rec)
                print(f"  [{len(results)}/{args.crawl}] {rec.get('url')}", file=sys.stderr)
        if frontier and len(results) < args.crawl and attempts >= max_attempts:
            note = f"达到抓取次数上限 {max_attempts},仍有 {len(frontier)} 个链接未抓取"
            print(f"  [预算耗尽] {note}", file=sys.stderr)
            if results:
                results[-1].setdefault("notes", []).append(note)
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, u): u for u in urls}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(failed(url, exc))
            if len(urls) > 1:
                print(f"  [{len(results)}/{len(urls)}] {url}", file=sys.stderr)
    return results


def save(records, outdir, args):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    index = []
    used = {}
    for rec in records:
        rec.pop("_fp", None)          # 内部字段不落盘
        base = slugify(rec["url"])
        slug = base
        # 不同的 URL 会撞同一个 slug:a.com / a.com/ / a.com/index 都得到 a.com__index,
        # a/b?x=1 与 a/b 也是。不处理的话后写静默覆盖先写,而 index.json 仍然列 N 条
        # 记录指向同一个文件 —— 看到的和落在磁盘上的不是一回事。
        if slug in used:
            stem = f"{base}-{hashlib.sha1(rec['url'].encode('utf-8')).hexdigest()[:6]}"
            slug, suffix = stem, 2
            while slug in used:
                slug = f"{stem}-{suffix}"
                suffix += 1
        used[slug] = rec["url"]
        (out / f"{slug}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        if rec.get("markdown"):
            (out / f"{slug}.md").write_text(rec["markdown"], encoding="utf-8")
        index.append({"url": rec["url"], "status": rec.get("status"), "backend": rec.get("backend"),
                      "verdict": rec.get("verdict"), "title": rec.get("meta", {}).get("title"),
                      "file": f"{slug}.json", "error": rec.get("error")})
    (out / "index.json").write_text(
        json.dumps({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "count": len(index), "pages": index}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return out


def parse_cookies(args):
    """Cookie 三种来源:显式串、JSON 文件、Netscape cookies.txt。"""
    cookies = {}
    if args.cookie:
        for part in args.cookie.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                cookies[key.strip()] = value.strip()
    if args.cookie_file:
        path = Path(args.cookie_file)
        if not path.is_file():
            print(f"[!] cookie 文件不存在: {path}", file=sys.stderr)
            return cookies
        text = path.read_text(encoding="utf-8", errors="replace")
        parsed = False
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                cookies.update({str(k): str(v) for k, v in data.items()})
                parsed = True
        except json.JSONDecodeError:
            pass
        if not parsed:
            for line in text.splitlines():
                line = line.rstrip("\n")
                if not line.strip() or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) >= 7:              # Netscape: domain flag path secure expiry name value
                    cookies[fields[5]] = fields[6]
                elif "=" in line:
                    key, value = line.split("=", 1)
                    cookies[key.strip()] = value.strip()
    return cookies


def load_proxies(args):
    proxies = []
    if args.proxy:
        proxies.append(args.proxy.strip())
    if args.proxy_file:
        path = Path(args.proxy_file)
        if path.is_file():
            proxies += [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                        if ln.strip() and not ln.startswith("#")]
        else:
            print(f"[!] 代理文件不存在: {path}", file=sys.stderr)
    return proxies


def fetch_with_wait_human(engine, url, args, scout_mod, timeout):
    """常规抓 → 受阻等人工 → 恢复自动;超时保留自动结果并明确标记降级。"""
    res = engine.fetch(url, backend=args.backend, scout_mod=scout_mod)

    def content_available(html):
        if args.css and html:
            try:
                return any(n.text() for n in ex.MiniDOM(html).select(args.css))
            except Exception:
                pass
        return None

    def human_state(result, status, reason):
        # Result 非 slots;仅在本链路附加状态,不改通用引擎/已有后端接口。
        result.degraded = status in ("timeout", "failed")
        result.human_status = status
        result.human_reason = str(reason)
        return result

    blocked, reason = ho.need_human(res.html, res.status, url,
                                    content_ok=content_available(res.html))
    if not blocked:
        return res

    print(f"[i] 检测到「{reason}」—— 停下等人,不再盲目重试", file=sys.stderr)
    try:
        r = ho.handoff_fetch(url, timeout=timeout, cdp=args.cdp,
                             profile=args.browser_profile,
                             save_state=str(ho.default_state_path(url)),
                             verbose=True,
                             keep_alive=getattr(args, "keep_browser", False))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:120]}"
        res.notes.append(f"接管未成功: {detail};已降级为自动结果")
        return human_state(res, "failed", detail)

    if r.get("degraded"):
        res.notes.append(f"人工等待超时,已降级为自动结果({reason})")
        return human_state(res, "timeout", reason)

    if r.get("ok"):
        # 先保留人工已取得的好内容。重抓比较对象应是它,不是原受阻页面。
        html = r.get("html", "")
        out = eng.Result(url=url, final_url=r.get("url") or url,
                         status=200, backend="handoff", html=html,
                         bytes=len(html.encode("utf-8")))
        out.notes.append(f"人工接管({reason})")
        new_cookies = r.get("cookies") or {}
        if new_cookies:
            try:
                engine.cookies_dict.update(new_cookies)
                engine._cffi = eng.make_cffi_session(engine.impersonate, engine.proxies,
                                                     engine.cookies_dict, engine.timeout)
                res2 = engine.fetch(url, backend=args.backend, scout_mod=scout_mod)
                if res2.error:
                    detail = res2.error[:50]
                elif not 200 <= res2.status < 300:
                    detail = f"HTTP {res2.status}"
                elif not res2.html:
                    detail = "空响应"
                else:
                    still_blocked, why = ho.need_human(
                        res2.html, res2.status, res2.final_url or url,
                        content_ok=content_available(res2.html))
                    if (not still_blocked and eng.visible_text_len(res2.html)
                            >= eng.visible_text_len(out.html)):
                        res2.notes.append(f"人工处理「{reason}」后自动重抓")
                        return human_state(res2, "completed", reason)
                    detail = why if still_blocked else "重抓正文少于接管内容"
                out.notes.append(f"接管后重抓未改善({detail}),沿用接管结果")
            except Exception as exc:
                out.notes.append(f"接管后重抓异常({type(exc).__name__}: {str(exc)[:50]}),沿用接管结果")
        return human_state(out, "completed", reason)

    detail = str(r.get("reason", ""))[:120]
    res.notes.append(f"接管未成功: {detail};已降级为自动结果")
    return human_state(res, "failed", detail or reason)


def states_dir():
    return Path.home() / ".armory" / "states"


def cmd_state_scan(timeout=20, online=True):
    """体检所有存档登录态:离线看文件本身,在线看凭证还认不认。

    存档散在 ~/.armory/states 里,失效是静默的 —— 不主动探一次,你会在
    半夜的长任务里才发现。这条命令就是「开跑前的点名」。
    """
    d = states_dir()
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    if not files:
        print(f"没有存档。人工接管时用 --handoff 存第一份: {d}")
        return 0

    rows = []
    for f in files:
        offline = ho.state_status(f)
        if online:
            valid, reason, method = auth_mod.probe_state(
                f"https://{f.stem}/", f, timeout=timeout)
            label = {True: "有效", False: "已失效", None: "无法判定"}[valid]
            tag = f"[{method}]" if method != "none" else ""
        else:
            label, reason, tag = "(未探测)", "", ""
        rows.append((f.stem, label, tag, reason, offline))

    w = max(len(r[0]) for r in rows)
    print(f"{'站点':<{w}}  {'在线':<12}{'离线状态'}")
    print("-" * (w + 62))
    for domain, label, tag, reason, offline in rows:
        print(f"{domain:<{w}}  {label + tag:<12}{offline}")
        if reason:
            print(f"{'':<{w}}  └ {reason}")

    dead = [r[0] for r in rows if r[1] == "已失效"]
    if dead:
        print(f"\n{len(dead)} 个存档已失效: {', '.join(dead)}")
        print("重新走一次人工接管即可续上: harvest.py <url> --handoff")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="harvest —— 爬取执行器(scout 判定驱动后端选择)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="URL 或包含 URL 的文件")
    ap.add_argument("--check", action="store_true", help="列出本机可用后端")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "static", "render", "stealth", "camoufox", "scrapling"])
    ap.add_argument("--format", default="markdown", choices=["markdown", "json", "text"])
    ap.add_argument("--json", action="store_true", help="等同 --format json")
    ap.add_argument("--css", help="CSS 选择器,抽匹配元素的文本")
    ap.add_argument("--regex", help="正则抽值")
    ap.add_argument("--regex-on-text", action="store_true", help="正则在纯文本上跑,而非原始 HTML")
    ap.add_argument("--links", action="store_true", help="带上全部链接")
    ap.add_argument("--tables", action="store_true", help="带上表格数据")
    ap.add_argument("--crawl", type=int, default=0, metavar="N", help="同域深度爬取,最多 N 页")
    ap.add_argument("--max-crawl-attempts", type=int, default=None, metavar="N",
                    help="深爬实际请求 URL 次数上限(含重复内容;默认页数的 10 倍)")
    ap.add_argument("--crawl-any-host", action="store_true", help="深爬时不限域名")
    ap.add_argument("--profile", default=eng.DEFAULT_PROFILE, choices=list(eng.PROFILES),
                    help="开箱预设。aggressive=不限速不限并发不查 robots; "
                         "balanced=2rps/8并发/查 robots; conservative=0.5rps/2并发/不keepalive")
    ap.add_argument("--concurrency", type=int, default=None, help="并发线程数")
    ap.add_argument("--rate", type=float, default=None, help="每域名每秒请求数,0=不限速")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--retries", type=int, default=None)
    ap.add_argument("--max-body", type=int, default=None, help="单响应体上限(字节)")
    ap.add_argument("--proxy", help="代理地址,如 http://127.0.0.1:8080")
    ap.add_argument("--proxy-file", help="代理列表文件,每行一个,按请求轮换")
    ap.add_argument("--proxy-check-url", help="代理健康检测目标(默认 api.ipify.org)")
    ap.add_argument("--proxy-no-sticky", dest="proxy_sticky", action="store_false", default=True,
                    help="关闭代理会话亲和(默认同站点保持同一出口,换得太勤反而可疑)")
    ap.add_argument("--check-proxies", action="store_true",
                    help="只做代理健康检测并打印报表,不抓取")
    ap.add_argument("--cookie", help='Cookie 串,形如 "session=abc; uid=1"')
    ap.add_argument("--cookie-file", help="Cookie 文件:JSON 对象 / Netscape cookies.txt / key=value 行")
    ap.add_argument("--state", help="登录状态文件(storage_state JSON)。存在则加载 cookie,"
                                    "配合 --login-* 时作为保存目标")
    ap.add_argument("--state-info", action="store_true", help="查看状态文件内容后退出")
    ap.add_argument("--state-scan", action="store_true",
                    help="体检全部存档登录态(离线状态 + 在线有效性),不抓取")
    ap.add_argument("--state-scan-offline", action="store_true",
                    help="配合 --state-scan:只做离线检查,不发请求")
    ap.add_argument("--browser-close", action="store_true",
                    help="清理所有 armory 起的浏览器进程(含历史遗留)后退出")
    ap.add_argument("--keep-browser", action="store_true",
                    help="自己启动的浏览器不自动关闭;默认用完就关")
    ap.add_argument("--no-backend-memory", action="store_true",
                    help="不读也不写后端记忆(~/.armory/backend_memory.json),每次从完整梯子重试")
    ap.add_argument("--state-check", nargs="?", const="auto", metavar="URL",
                    help="主动探测登录态是否仍有效(不抓取)。不给 URL 时自动选探针:"
                         "站点有身份接口就打接口,否则带登录态与匿名各取一次做差分")
    ap.add_argument("--auto-relogin", action="store_true",
                    help="登录态失效时自动重登(需同时给 --login-url/--login-user/--login-pass)")
    ap.add_argument("--login-url", help="登录页 URL。给定时先用浏览器登录一次再抓取")
    ap.add_argument("--login-user", help="登录账号")
    ap.add_argument("--login-pass", help="登录密码")
    ap.add_argument("--login-user-field", help="账号输入框选择器(自动探测失败时用)")
    ap.add_argument("--login-pass-field", help="密码输入框选择器")
    ap.add_argument("--login-submit-field", help="提交按钮选择器")
    ap.add_argument("--headful", action="store_true", help="显示浏览器窗口(需要人工过验证码/扫码时)")
    ap.add_argument("--keep-open", type=float, default=0.0,
                    help="提交后保持浏览器打开 N 秒,留给人工处理验证码")
    ap.add_argument("--impersonate", default="chrome",
                    help="curl_cffi 浏览器指纹(chrome/chrome120/chrome116/safari/firefox/edge 等),"
                         "默认 chrome。让 JA3/TLS 指纹与真实浏览器一致")
    ap.add_argument("--no-impersonate", dest="prefer_cffi", action="store_false", default=True,
                    help="关闭 curl_cffi,改用自建连接池(JA3 会暴露为 Python)")
    ap.add_argument("--rotate-ua", dest="rotate_ua", action="store_true", default=None,
                    help="每个请求轮换 User-Agent")
    ap.add_argument("--no-rotate-ua", dest="rotate_ua", action="store_false")
    ap.add_argument("--keepalive", dest="keepalive", action="store_true", default=None,
                    help="复用 TCP/TLS 连接(默认开)")
    ap.add_argument("--no-keepalive", dest="keepalive", action="store_false")
    ap.add_argument("--respect-robots", dest="respect_robots", action="store_true", default=None,
                    help="遵守 robots.txt(默认关)")
    ap.add_argument("--solve-captcha", metavar="IMAGE_URL",
                    help="下载该图片并用 ddddocr 识别(图形验证码),打印结果后退出")
    ap.add_argument("--sniff", action="store_true",
                    help="渲染页面并记录全部网络请求,回答「数据从哪来」")
    ap.add_argument("--wait-human", type=int, nargs="?", const=300, default=None,
                    metavar="SECONDS",
                    help="撞上登录墙/验证时停下等你处理(默认 300s);处理完自动继续,"
                         "等满超时则降级回全自动 —— 你不在也不耽误")
    ap.add_argument("--handoff", action="store_true",
                    help="人工接管模式:卡在验证(人机验证/滑块/登录墙)时暂停等你处理,"
                         "你在浏览器里点完,程序自动继续")
    ap.add_argument("--handoff-timeout", type=int, default=300,
                    help="等待人工处理的秒数(默认 300)")
    ap.add_argument("--domain-state", action="store_true",
                    help="自动加载 ~/.armory/states/<域名>.json 里存档的登录态"
                         "(接管过一次留下的,后续静态抓取直接复用)")
    ap.add_argument("--browser-info", action="store_true", help="查看有没有可接管的浏览器")
    ap.add_argument("--browser-start", action="store_true",
                    help="以调试模式启动浏览器(带独立 profile,保留登录态)")
    ap.add_argument("--browser-profile", help="接管用的 profile 目录(默认 ~/.armory/browser-profile)")
    ap.add_argument("--browser-port", type=int, default=9222, help="调试端口(默认 9222)")
    ap.add_argument("--cdp", help="直接指定已运行浏览器的 CDP endpoint")
    ap.add_argument("--sniff-wait", type=int, default=6000, help="嗅探等待毫秒数")
    ap.add_argument("--sniff-all", action="store_true", help="连静态资源一起列出")
    ap.add_argument("-o", "--out", help="落盘目录")
    ap.add_argument("--max-chars", type=int, default=6000, help="正文截断长度")
    args = ap.parse_args()
    if args.crawl < 0 or (args.max_crawl_attempts is not None and args.max_crawl_attempts <= 0):
        ap.error("--crawl 不能为负,--max-crawl-attempts 必须为正数")
    if args.json:
        args.format = "json"          # 与 scout 的 --json 保持一致

    prof = eng.PROFILES[args.profile]

    if args.browser_close:
        ho.kill_stale_browsers()
        return 0

    if args.state_scan:
        return cmd_state_scan(timeout=args.timeout, online=not args.state_scan_offline)

    if args.state_info:
        if not args.state or not Path(args.state).is_file():
            print(f"[!] 状态文件不存在: {args.state}", file=sys.stderr)
            return 1
        print(auth_mod.describe_state(args.state))
        return 0

    if args.check:
        print("可用后端:")
        for name, ok in eng.available_backends().items():
            mark = "✓" if ok else "✗"
            hint = "" if ok else "   (pip install playwright && playwright install chromium)"
            print(f"  {mark} {name}{hint if name == 'render' else ''}")
        print(f"\n预设 {args.profile}:")
        for key, value in prof.items():
            print(f"  {key:<15} {value}")
        print(f"\n可用开关: --rate --concurrency --rotate-ua --keepalive --respect-robots "
              f"--proxy --cookie --max-body")
        return 0

    if not args.target and not args.login_url and not args.solve_captcha \
            and not args.check_proxies and not args.state_info and not args.sniff \
            and not args.state_check and not args.browser_info and not args.browser_start:
        ap.error("需要 URL 或 URL 文件(或用 --login-url / --solve-captcha / "
                 "--check-proxies / --state-info / --sniff / --state-check / "
                 "--state-scan / --browser-close / --browser-info / --browser-start)")

    urls = []
    if args.target:
        target_path = Path(args.target)
        if target_path.is_file():
            urls = [normalize(u.strip())
                    for u in target_path.read_text(encoding="utf-8").splitlines()
                    if u.strip() and not u.startswith("#")]
        else:
            urls = [normalize(args.target)]
        if not urls:
            ap.error("没有可用的 URL")

    # 未显式指定的项取预设值 —— 限制都在,只是默认按预设放开
    def pick(name):
        value = getattr(args, name)
        return prof.get(name) if value is None else value

    # 浏览器管理放在引擎构造之前 —— 这两个分支不需要抓取引擎,提前返回更干净
    if args.browser_info:
        found = ho.find_debug_browser()
        if found:
            print(f"可接管: {found['browser']}\n  endpoint: {found['endpoint']}")
            print(f"\n用这个跑: harvest.py <url> --handoff --cdp {found['endpoint']}")
        else:
            exe = ho.chromium_path() or "<浏览器路径>"
            print("没有发现以调试模式运行的浏览器。两种做法:")
            print("  1) harvest.py --browser-start        # 用独立 profile 起一个")
            print("  2) 手动启动你自己的浏览器:")
            print(f"     {exe} --remote-debugging-port={args.browser_port} "
                  f"--user-data-dir=~/.armory/browser-profile")
        return 0

    if args.browser_start:
        try:
            _, endpoint = ho.launch_debug_browser(profile_dir=args.browser_profile,
                                                  port=args.browser_port)
        except RuntimeError as exc:
            print(f"[!] {exc}", file=sys.stderr)
            return 1
        print(f"\n浏览器已启动。在里面登录、过验证,然后:")
        print(f"  harvest.py <url> --handoff --cdp {endpoint}")
        return 0

    proxies = load_proxies(args)
    cookies = parse_cookies(args)

    # 复用接管时存档的登录态:一次人工介入,之后这个域名都走高速静态路径
    if args.domain_state:
        if urls:
            dc, dp = ho.load_domain_state(urls[0])
            if dc:
                cookies.update(dc)
                print(f"[i] 复用存档登录态 {dp.name}({len(dc)} 项 cookie)", file=sys.stderr)
            elif dp:
                # 有存档却没加载 —— 必须说清为什么,不然只会看到"抓不到"这个现象
                print(f"[i] 存档未加载: {ho.state_status(dp)}", file=sys.stderr)
                print("    重新接管一次即可刷新: harvest.py <url> --handoff --cdp <endpoint>",
                      file=sys.stderr)
            else:
                print(f"[i] {urls[0]} 没有存档登录态;先跑一次 --handoff 建档", file=sys.stderr)

    # 登录是低频、复杂、易变的一步(CSRF / JS 加密 / 验证码 / 扫码);
    # 抓取是高频、要求快的一步。拆开之后:浏览器登一次,cookie 交给静态后端,
    # 之后每页 0.12s,不必每次渲染。
    if args.login_url:
        if not (args.login_user and args.login_pass):
            ap.error("--login-url 需要同时给 --login-user 与 --login-pass")
        if not args.state:
            ap.error("--login-url 需要 --state 指定状态文件保存路径")
        print(f"[i] 浏览器登录 {args.login_url}", file=sys.stderr)
        try:
            auth_mod.login(args.login_url, args.login_user, args.login_pass, args.state,
                           headless=not args.headful, timeout=args.timeout,
                           user_field=args.login_user_field,
                           pass_field=args.login_pass_field,
                           submit_field=args.login_submit_field,
                           keep_open=args.keep_open)
        except auth_mod.LoginError as exc:
            print(f"[!] 登录失败: {exc}", file=sys.stderr)
            return 1
        if not args.target:
            return 0

    if args.state and Path(args.state).is_file():
        state_cookies, meta = auth_mod.load_state(args.state)
        if meta.get("age_hours") is not None:
            print(f"[i] 登录态来自 {args.state}({meta['age_hours']} 小时前,"
                  f"{len(state_cookies)} 个 cookie)", file=sys.stderr)
        cookies.update(state_cookies)

    # 探活 + 自动续期:长任务里会话过期是必然的,与其半夜静默失败不如开跑前先探一次
    if args.state_check:
        probe_url = args.target if args.state_check == "auto" else args.state_check
        if not probe_url:
            ap.error("--state-check 不给 URL 时需要同时给一个目标 URL 当探针")
        valid, reason, method = auth_mod.probe_state(
            probe_url, args.state, cookies=cookies, timeout=args.timeout)
        relogged = False
        if valid is False and args.auto_relogin and args.login_url:
            if not (args.login_user and args.login_pass):
                ap.error("--auto-relogin 需要 --login-url/--login-user/--login-pass")
            print(f"[i] 登录态已失效,自动重登 {args.login_url}", file=sys.stderr)
            auth_mod.login(args.login_url, args.login_user, args.login_pass, args.state,
                           headless=not args.headful, timeout=args.timeout)
            relogged = True
            valid, reason, method = auth_mod.probe_state(
                probe_url, args.state, timeout=args.timeout)
        label = {True: "有效", False: "已失效", None: "无法判定"}[valid]
        result = {"valid": valid, "reason": reason, "method": method, "relogged": relogged}
        # 有 target 时后面还要输出抓取结果,探测结论走 stderr 免得污染 --json 管道
        sink = sys.stderr if args.target else sys.stdout
        print(json.dumps(result, ensure_ascii=False) if args.format == "json"
              else (f"登录态: {label}{'(已自动续期)' if relogged else ''} — {reason}"
                    f"  [{method}]"), file=sink)
        if not args.target:
            return 0 if valid else 1
        if relogged and args.state:
            state_cookies, _ = auth_mod.load_state(args.state)
            cookies.update(state_cookies)
    engine = eng.Engine(
        rate=pick("rate"),
        timeout=args.timeout,
        retries=pick("retries"),
        proxies=proxies,
        respect_robots=pick("respect_robots"),
        rotate_ua=pick("rotate_ua"),
        keepalive=pick("keepalive"),
        cookies=cookies,
        max_body=args.max_body or 10_000_000,
        max_redirects=prof.get("max_redirects", 10),
        impersonate=args.impersonate,
        prefer_cffi=args.prefer_cffi,
        proxy_check_url=args.proxy_check_url,
        proxy_sticky=args.proxy_sticky,
        backend_memory=not args.no_backend_memory,
    )
    if args.concurrency is None:
        args.concurrency = prof.get("concurrency", 8)

    if proxies:
        print(f"[i] 代理 {len(proxies)} 个,按请求轮换", file=sys.stderr)
    if cookies:
        print(f"[i] 注入 Cookie {len(cookies)} 项", file=sys.stderr)
    if engine._cffi is not None:
        print(f"[i] 指纹伪装: curl_cffi/{args.impersonate}(JA3 与真实浏览器一致)",
              file=sys.stderr)

    if args.handoff:
        if not urls:
            print("[!] 接管模式需要 URL", file=sys.stderr)
            return 1
        records = []
        for u in urls:
            # 默认按域名存档 —— 人工过完验证拿到的会话不该只用一次,
            # 之后同域名可以直接走静态路径复用
            state_path = args.state or str(ho.default_state_path(u))
            r = ho.handoff_fetch(u, timeout=args.handoff_timeout, cdp=args.cdp,
                                 profile=args.browser_profile,
                                 save_state=state_path,
                                 verbose=True,
                                 keep_alive=args.keep_browser)
            if r.get("ok"):
                res = eng.Result(url=r["url"], status=200, backend="handoff",
                                 html=r.get("html", ""), bytes=len(r.get("html", "")),
                                 final_url=r["url"])
                res.notes.append(f"人工接管({r['reason']})" if r.get("handled")
                                 else "无需人工介入")
                records.append(build_record(res, args))
            else:
                records.append({"url": u, "error": r.get("reason", "接管失败"),
                                "backend": "handoff"})
        for rec in records:
            if rec.get("error"):
                # JSON 模式下错误也必须是 JSON —— 否则管道另一边解析直接炸
                print(json.dumps(rec, ensure_ascii=False, indent=2)
                      if args.format == "json" else f"[!] {rec['url']}: {rec['error']}")
            else:
                print(render_human(rec, args))
            print()
        if args.out:
            print(f"[+] 落盘 → {save(records, args.out, args)}", file=sys.stderr)
        return 0 if records and all(record_ok(rec) for rec in records) else 1

    if args.sniff:
        if not urls:
            print("[!] 需要 URL", file=sys.stderr)
            return 1
        target = urls[0]
        print(f"[i] 嗅探 {target}(渲染 {args.sniff_wait}ms 并记录全部请求)", file=sys.stderr)
        sniffs, engine_used = engine.fetch_sniff(target, wait_ms=args.sniff_wait)
        if sniffs is None:
            print(f"[!] 嗅探失败: {engine_used}", file=sys.stderr)
            return 1
        apis = eng.Engine.summarize_sniff(sniffs, only_api=not args.sniff_all)
        hosts = {}
        for a in apis:
            hosts.setdefault(a["host"], []).append(a)

        print(f"\n渲染引擎: {engine_used} | 总请求 {len(sniffs)} | 接口类 {len(apis)}\n")
        for host, items in sorted(hosts.items(), key=lambda kv: -len(kv[1])):
            print(f"── {host}  ({len(items)})")
            for a in items:
                fid = f" [{a['function_id']}]" if a["function_id"] else ""
                status = f"{a['status']}" if a["status"] else "-"
                print(f"   {a['method']:<5} {status:<4} {a['url'][:110]}{fid}")
                if a["body_sample"] and args.sniff_all:
                    print(f"         body: {a['body_sample'][:100]}")
            print()
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            payload = {"url": target, "engine": engine_used,
                       "total_requests": len(sniffs), "apis": apis}
            (out / f"sniff_{slugify(target)}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[+] 详情已存: {out}/sniff_{slugify(target)}.json", file=sys.stderr)
        return 0

    if args.check_proxies:
        if not proxies:
            print("[!] 没给代理(--proxy / --proxy-file)", file=sys.stderr)
            return 1
        alive = engine.proxy_pool.check_all() if engine.proxy_pool else 0
        print(f"\n可用 {alive} / {len(proxies)}\n")
        print(engine.proxy_pool.render() if engine.proxy_pool else "")
        return 0

    if args.solve_captcha:
        try:
            import ddddocr
        except ImportError:
            print("[!] 验证码识别需要 ddddocr: pip install ddddocr", file=sys.stderr)
            return 1
        try:
            status, data = engine.fetch_binary(args.solve_captcha)
        except Exception as exc:
            print(f"[!] 图片下载失败: {exc}", file=sys.stderr)
            return 1
        if status >= 400 or not data:
            print(f"[!] 图片下载失败: status={status} bytes={len(data)}", file=sys.stderr)
            return 1
        print(ddddocr.DdddOcr(show_ad=False).classification(data))
        return 0

    if args.crawl:
        args.crawl_same_host = not args.crawl_any_host
        # 与单页共用 Engine.fetch:避免异步静态快径静默绕过 auto 渲染、
        # robots、响应体上限与人工接管。worker 仍保留分层线程并发。
        records = worker(args, urls, engine)
    else:
        records = asyncio.run(_batch(args, urls, engine))

    ok = sum(1 for rec in records if record_ok(rec))
    exit_code = 0 if records and ok == len(records) else 1
    if args.out or len(records) > 5:
        # 多页结果只打摘要 —— 深爬几十页时把正文全打到终端没有意义
        print(f"[+] {ok}/{len(records)} 成功", file=sys.stderr)
        for rec in records[:40]:
            line = f"    {rec.get('status', '-'):<4} {rec.get('backend', '-'):<9} {rec['url'][:90]}"
            if rec.get("error"):
                line += f"  ! {rec['error'][:50]}"
            print(line, file=sys.stderr)
        if len(records) > 40:
            print(f"    …另有 {len(records) - 40} 页", file=sys.stderr)
        if args.out:
            out = save(records, args.out, args)
            print(f"[+] 落盘 → {out}", file=sys.stderr)
        return exit_code

    for rec in records:
        print(render_human(rec, args))
        print()
    return exit_code


async def _batch(args, urls, engine):
    loop = asyncio.get_event_loop()
    if len(urls) == 1:
        args.crawl_same_host = True
        return await loop.run_in_executor(None, worker, args, urls, engine)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as pool:
        return await loop.run_in_executor(pool, worker, args, urls, engine)


if __name__ == "__main__":
    sys.exit(main())
