#!/usr/bin/env python3
"""e2e_10sites —— 端到端验收:中美各 5 站,要求 10/10 成功且产出高质量 Markdown

「成功」不等于 HTTP 200。拦截页、登录墙、JS 空壳都会返回 200,什么都不说。
所以这里按五个维度打分:

    1. 传输层  无异常、状态码正常、拿到内容
    2. 拦截层  不出现人机验证/机器人检测/访问拒绝特征
    3. 内容量  Markdown 正文达到可用长度(默认 800 字符)
    4. 纯净度  正文占全文比例,噪声(导航/页脚)不能压倒主体
    5. 结构    Markdown 里有标题与成段文本,不是一行糊到底

用法:
    python3 tools/e2e_10sites.py                    # 全量
    python3 tools/e2e_10sites.py --only cn          # 只跑中国站
    python3 tools/e2e_10sites.py --site books.toscrape.com
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "modules" / "harvest"))
sys.path.insert(0, str(ROOT / "modules" / "scout"))

import engine as eng          # noqa: E402
import extract as ex          # noqa: E402
import scout as scout_mod     # noqa: E402

# 中美各 5 家,按防护难度从低到高排 —— 只看「能不能抓」没有意义,
# 要看「防护等级不同的站点能不能都用同一套流程覆盖」
SITES = [
    ("us", "https://books.toscrape.com/", "静态练习站", "低"),
    ("us", "https://quotes.toscrape.com/js/", "JS 渲染练习站", "中"),
    ("us", "https://news.ycombinator.com/", "Hacker News 首页", "低"),
    ("us", "https://en.wikipedia.org/wiki/Web_scraping", "维基百科条目", "低"),
    ("us", "https://arstechnica.com/", "Ars Technica 首页", "中高"),

    ("cn", "https://www.cnblogs.com/", "博客园首页", "低"),
    ("cn", "https://www.oschina.net/", "开源中国首页", "中"),
    ("cn", "https://www.36kr.com/", "36氪首页", "中高"),
    ("cn", "https://www.jianshu.com/", "简书首页", "中高"),
    # 知乎的文章页在无登录态下连 camoufox 都返回 403(登录墙 + 深度风控),
    # 专栏首页是唯一匿名可达且有实质内容的入口
    ("cn", "https://zhuanlan.zhihu.com/", "知乎专栏(匿名可达部分)", "高"),
]

# 拦截特征。命中即判失败 —— 这类页面全是 200,只靠状态码看不出来
BLOCK_SIGNATURES = [
    ("verify you are human", "人机验证"),
    ("checking your browser", "CF 挑战"),
    ("enable javascript", "要求 JS"),
    ("just a moment", "等待跳转"),
    ("access denied", "访问拒绝"),
    ("请求过于频繁", "频率拦截"),
    ("安全验证", "安全验证"),
    ("请先登录", "登录墙"),
    ("登录后查看", "登录墙"),
    ("robot check", "机器人检测"),
    ("captcha", "验证码"),
]

MIN_CHARS = 800


def assess(res, md, min_chars=MIN_CHARS):
    """五维打分。返回 (是否通过, 问题列表, 指标字典)。"""
    issues = []
    stats = {}

    if res.error:
        return False, [f"传输失败: {res.error[:60]}"], stats
    if not isinstance(res.status, int) or not 200 <= res.status < 300:
        issues.append(f"HTTP {res.status}")
    if not res.html:
        issues.append("响应体为空")

    # 拦截特征只在标题和正文开头判定 —— 一篇讲爬虫的文章正文里本来就会有
    # 「captcha」「robot」这些词,拿全文 grep 会把正常页面判成拦截页
    title = ""
    if res.html:
        m = re.search(r"<title[^>]*>(.*?)</title>", res.html, re.S | re.I)
        title = re.sub(r"\s+", " ", m.group(1)) if m else ""
    head_text = (title + " " + md[:400]).lower()
    for sig, name in BLOCK_SIGNATURES:
        if sig in head_text:
            issues.append(f"疑似拦截({name})")
            break

    stats["md_chars"] = len(md)
    # Markdown 链接目标、图片和裸 URL 不是可读正文,不能靠长地址凑阈值。
    readable = re.sub(r"!\[[^\]\n]*\]\([^\n]*?\)", "", md)
    readable = re.sub(r"\[([^\]\n]*)\]\([^\n]*?\)", r"\1", readable)
    readable = re.sub(r"(?m)^\s*\[[^\]]+\]:\s+\S+.*$", "", readable)
    readable = re.sub(r"(?:https?://|data:)\S+", "", readable)
    readable = re.sub(r"[#*_`>|]+", "", readable)
    text_chars = len(ex.html_to_text(res.html)) if res.html else 0
    content_chars = min(len(re.sub(r"\s+", " ", readable).strip()), text_chars)
    stats["content_chars"] = content_chars
    if content_chars < min_chars:
        issues.append(f"正文过短({content_chars}<{min_chars})")
    stats["text_chars"] = text_chars
    stats["purity"] = round(content_chars / text_chars, 2) if text_chars else 0.0

    headings = len(re.findall(r"^#{1,6} ", md, re.M))
    paras = len([p for p in md.split("\n\n") if len(p.strip()) > 80])
    list_items = len(re.findall(r"^[-*\d] ", md, re.M))
    table_rows = len(re.findall(r"^\|.*\|$", md, re.M))
    nonblank = len([ln for ln in md.split("\n") if ln.strip()])
    # 噪声:内联 base64 数据(占位图)和裸链接占比过高就不是给人读的文档
    inline_data = sum(len(m) for m in re.findall(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+", md))
    stats.update({"headings": headings, "paragraphs": paras, "list_items": list_items,
                  "table_rows": table_rows, "nonblank_lines": nonblank,
                  "inline_data_bytes": inline_data})
    if inline_data > 2000:
        issues.append(f"内联 base64 噪声 {inline_data} 字节")
    # 结构判定:标题/长段落/列表/表格任一成规模即可;退一步,足够多的独立行也算 ——
    # 链接聚合页(如 HN)就是「编号行 + 内容行」的模式,本来就不该有标题和长段落
    if not (headings >= 1 or paras >= 3 or list_items >= 10
            or table_rows >= 10 or nonblank >= 30):
        issues.append(f"结构单薄(标题{headings} 段落{paras} 列表{list_items} 行{nonblank})")

    return not issues, issues, stats


def fetch_one(asset, url, use_camoufox_fallback=True, min_chars=MIN_CHARS):
    """抓一个站点。auto 升级失败时,再试指纹级后端。"""
    e = asset["engine"]
    res = e.fetch(url, backend="auto", scout_mod=scout_mod)
    md = ex.html_to_markdown(res.html) if res.html else ""
    ok, issues, stats = assess(res, md, min_chars=min_chars)

    if not ok and use_camoufox_fallback and eng.has_module("camoufox"):
        res2 = e.fetch_camoufox(url)
        md2 = ex.html_to_markdown(res2.html) if res2.html else ""
        ok2, issues2, stats2 = assess(res2, md2, min_chars=min_chars)
        if (len(md2) > len(md)) or (ok2 and not ok):
            return res2, md2, ok2, issues2, stats2, "升级 camoufox"
    return res, md, ok, issues, stats, res.backend


def run_site(target, timeout, outdir, min_chars=MIN_CHARS):
    """一个站点的验收全流程。

    **线程内自建引擎** —— Engine 持有连接池与会话,不跨线程共享。
    """
    region, url, label, level = target
    asset = {"engine": eng.Engine(rate=0, timeout=timeout, retries=1,
                                  impersonate="chrome", settle_ms=1200)}
    host = url.split("//")[1].split("/")[0]
    t0 = time.time()
    try:
        res, md, ok, issues, stats, backend = fetch_one(asset, url, min_chars=min_chars)
    except Exception as exc:
        res, md, ok, issues, stats, backend = None, "", False, [f"异常: {exc}"], {}, "-"
    elapsed = time.time() - t0

    slug = re.sub(r"[^\w.-]+", "_", host)
    if md:
        (outdir / f"{slug}.md").write_text(md, encoding="utf-8")

    return {"region": region, "url": url, "label": label, "level": level,
            "host": host, "ok": ok, "issues": issues, "backend": backend,
            "stats": stats, "elapsed": round(elapsed, 2),
            "verdict": res.verdict if res else None,
            "markdown_file": f"{slug}.md" if md else None}


def main():
    ap = argparse.ArgumentParser(description="10 站端到端验收")
    ap.add_argument("--only", choices=["us", "cn"], help="只跑一侧")
    ap.add_argument("--site", help="只跑某个域名")
    ap.add_argument("--out", default="out/e2e", help="Markdown 输出目录")
    ap.add_argument("--min-chars", type=int, default=MIN_CHARS)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=4,
                    help="跨站点并行度(默认 4;1 = 串行)")
    args = ap.parse_args()

    if args.min_chars <= 0 or args.jobs <= 0 or args.timeout <= 0:
        ap.error("--min-chars、--jobs 与 --timeout 必须为正数")
    targets = SITES
    if args.only:
        targets = [s for s in targets if s[0] == args.only]
    if args.site:
        targets = [s for s in targets if args.site in s[1]]

    if not targets:
        ap.error("没有匹配的验收目标;不能把 0/0 视为通过")
    outdir = ROOT / args.out
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"端到端验收: {len(targets)} 站 | 正文阈值 {args.min_chars} 字符 | 并行 {args.jobs}\n")

    t_all = time.time()
    if args.jobs > 1:
        # 跨站点并行是安全的:不同主机,各自的限速互不干扰。
        # 同站多页仍由引擎内部串行限速,不会因为这里并行而对单站超频。
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(lambda t: run_site(t, args.timeout, outdir, args.min_chars), targets))
    else:
        results = [run_site(t, args.timeout, outdir, args.min_chars) for t in targets]
    wall = time.time() - t_all

    print(f"{'站点':<26} {'区域':<5} {'防护':<5} {'后端':<10} {'正文':>7} {'纯净':>5} "
          f"{'结构':>6}  {'耗时':>6}  结果")
    print("-" * 104)
    for r in results:
        note = "✓ 通过" if r["ok"] else "✗ " + "; ".join(r["issues"][:2])
        print(f"{r['host']:<26} {r['region']:<5} {r['level']:<5} {r['backend']:<10} "
              f"{r['stats'].get('content_chars', 0):>7} {r['stats'].get('purity', 0):>5} "
              f"{r['stats'].get('headings', 0):>3}/{r['stats'].get('paragraphs', 0):<2}  "
              f"{r['elapsed']:>5.1f}s  {note}")

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    print("-" * 96)
    print(f"通过 {passed}/{total}  |  墙钟 {wall:.1f}s")

    us = [r for r in results if r["region"] == "us"]
    cn = [r for r in results if r["region"] == "cn"]
    if us:
        print(f"  美国 {sum(1 for r in us if r['ok'])}/{len(us)}")
    if cn:
        print(f"  中国 {sum(1 for r in cn if r['ok'])}/{len(cn)}")

    failed = [r for r in results if not r["ok"]]
    if failed:
        print("\n未通过:")
        for r in failed:
            print(f"  {r['url']}\n    问题: {'; '.join(r['issues'])}")

    report = ROOT / args.out / "report.json"
    report.write_text(json.dumps({"passed": passed, "total": total, "min_chars": args.min_chars, "results": results},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {report}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
