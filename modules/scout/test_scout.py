#!/usr/bin/env python3
"""scout 自测 —— 不联网,用构造样本验证判定逻辑

    python3 modules/scout/test_scout.py

覆盖三类容易判错的情形:
  1. 标签膨胀的静态页(文本比低但数据在 HTML 里)vs JS 注入页(文本比同样低)
  2. 首屏有内容、目标数据却走 XHR 的页面
  3. 挂在 CDN 后面 ≠ 会被指纹检测
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scout  # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" (期望 {want!r})"))
    if not ok:
        FAILED.append(name)


def base_ev(**kw):
    ev = {
        "edges": {}, "captcha": {}, "js_challenge": {}, "spa": {}, "honeypot": {},
        "hidden_links": 0, "ajax": {"异步调用": False, "空数据容器": False, "加载占位": False},
        "rate_headers": {}, "embedded": {}, "api_hints": [], "request_level": "低",
        "request_evidence": [], "text_ratio": 0.30, "script_ratio": 0.05,
        "html_bytes": 10_000, "cdn_challenge": [], "login_required": False,
        "login_evidence": [],
    }
    ev.update(kw)
    return ev


def test_page_shape():
    print("\n[page_shape] 文本比与脚本占比的区分度")
    # 标签膨胀的静态页:大量属性、几乎无脚本
    static = '<html><body>' + ''.join(
        f'<div class="item product-tile col-md-4" data-id="{i}" data-cat="books"><h3 class="t">{i}</h3></div>'
        for i in range(60)) + '</body></html>'
    tr, sr = scout.page_shape(static)
    check("静态页脚本占比 < 5%", sr < 0.05, True)

    # JS 注入页:脚本占大头、可见文本极少
    spa = ('<html><head><script>' + 'var x=1;' * 800 + '</script></head>'
           '<body><div id="app"></div><script>' + 'render();' * 400 + '</script></body></html>')
    tr2, sr2 = scout.page_shape(spa)
    check("JS 页脚本占比 > 50%", sr2 > 0.5, True)
    check("JS 页文本比 < 5%", tr2 < 0.05, True)
    check("两者文本比接近但脚本占比拉开", (sr2 - sr) > 0.5, True)


def test_verdicts():
    print("\n[decide] 策略收敛")
    v, _, _, _ = scout.decide(base_ev())
    check("普通静态页", v, "静态直取")

    v, _, _, _ = scout.decide(base_ev(text_ratio=0.017, script_ratio=0.78))
    check("JS 注入页", v, "动态渲染")

    v, _, _, _ = scout.decide(base_ev(
        text_ratio=0.057, script_ratio=0.46,
        ajax={"异步调用": True, "空数据容器": True, "加载占位": False}))
    check("有内容但数据走 XHR", v, "动态渲染")

    v, _, _, _ = scout.decide(base_ev(embedded={"__NEXT_DATA__": {"bytes": 2048, "valid_json": True,
                                                                "top_keys": ["props"]}}))
    check("首屏内嵌可解析 JSON", v, "内嵌数据直取")

    v, why, _, _ = scout.decide(base_ev(captcha={"极验 Geetest": "geetest"}))
    check("验证码优先于一切", v, "验证码闸门")

    v, _, _, _ = scout.decide(base_ev(
        js_challenge={"JS Cookie 计算挑战": "__jsl_clearance"}))
    check("JS 挑战", v, "反检测渲染")

    v, _, _, _ = scout.decide(base_ev(captcha={"reCAPTCHA": "g-recaptcha"},
                                      embedded={"__NEXT_DATA__": {"bytes": 900, "valid_json": True,
                                                                  "top_keys": []}}))
    check("验证码压过内嵌数据", v, "验证码闸门")

    v, _, _, _ = scout.decide(base_ev(
        embedded={"window.__DATA__": {"bytes": 512, "valid_json": False, "top_keys": None}}))
    check("内嵌但解析失败时不当内嵌数据", v, "静态直取")

    v, _, _, _ = scout.decide(base_ev(spa={"React 挂载点": '<div id="root"'}, text_ratio=0.02))
    check("SPA 挂载点 + 低文本", v, "动态渲染")


def test_layers():
    print("\n[layers] 分层判定")
    _, _, layers, _ = scout.decide(base_ev(edges={"Cloudflare": ["cf-ray", "cf-cache-status"]}))
    check("挂 CDN 不等于指纹检测", layers["指纹层"]["level"], "低")

    _, _, layers, _ = scout.decide(base_ev(cdn_challenge=["cf_clearance 已下发"]))
    check("出现挑战凭证才判高", layers["指纹层"]["level"], "高")

    _, _, layers, _ = scout.decide(base_ev(request_level="高",
                                           request_evidence=["裸 UA 被拒 403"]))
    check("裸 UA 被拒 → 请求特征层高", layers["请求特征层"]["level"], "高")

    _, _, layers, _ = scout.decide(base_ev(rate_headers={"retry-after": "60"}))
    check("限流头被采信", layers["行为特征层"]["level"], "有显式限流头")

    _, _, layers, _ = scout.decide(base_ev(login_required=True, login_evidence=["状态码 401"]))
    check("登录态识别", layers["会话层"]["level"], "需登录态")


def test_signatures():
    print("\n[特征表] 正则命中")
    check("蜜罐诱饵字段",
          bool(re.search(scout.HONEYPOT_SIGNATURES["诱饵表单字段"],
                         '<input type="hidden" name="honeypot" value="">', re.I)), True)
    check("负偏移定位",
          bool(re.search(scout.HONEYPOT_SIGNATURES["负偏移定位"],
                         'style="position:absolute;left:-9999px"', re.I)), True)
    check("普通隐藏链接不算陷阱",
          bool(re.search(scout.HONEYPOT_SIGNATURES["负偏移定位"],
                         '<a style="display:none" href="/x">menu</a>', re.I)), False)
    check("空数据容器(带 id)",
          bool(re.search(scout.AJAX_SIGNATURES["空数据容器"],
                         '<tbody id="table-body">\n\n</tbody>', re.I)), True)
    check("普通非空表格不算异步容器",
          bool(re.search(scout.AJAX_SIGNATURES["空数据容器"],
                         '<tbody id="x"><tr><td>1</td></tr></tbody>', re.I)), False)
    check("边缘指纹:阿里 WAF",
          "阿里云 WAF" in scout.match_edges({"set-cookie": "acw_tc=abc; path=/"}), True)
    check("边缘指纹:Cloudflare server 头",
          "Cloudflare" in scout.match_edges({"server": "cloudflare"}), True)


def test_extract():
    print("\n[extract_embedded] 内嵌数据提取")
    html = '<html><script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{}}}</script></html>'
    out = scout.extract_embedded(html)
    check("__NEXT_DATA__ 被识别", "__NEXT_DATA__" in out, True)
    check("JSON 解析成功", out["__NEXT_DATA__"]["valid_json"], True)
    check("顶层键提取", out["__NEXT_DATA__"]["top_keys"], ["props"])
    check("坏 JSON 不抛异常",
          scout.extract_embedded('<script id="__NEXT_DATA__">{oops</script>')["__NEXT_DATA__"]["valid_json"],
          False)


def test_api_hints():
    print("\n[find_api_hints] 接口线索")
    html = '<script>fetch("/api/v2/items?page=1"); var u = "https://x.com/graphql"; </script>'
    hints = scout.find_api_hints(html)
    check("抽出 /api 路径", any("/api/v2/items" in h for h in hints), True)
    check("抽出 graphql 端点", any("graphql" in h for h in hints), True)
    check("无线索时返回空", scout.find_api_hints("<p>hello</p>"), [])


if __name__ == "__main__":
    print("scout 自测 (不联网)")
    test_page_shape()
    test_verdicts()
    test_layers()
    test_signatures()
    test_extract()
    test_api_hints()
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {', '.join(FAILED)}")
        sys.exit(1)
    print("全部通过")
