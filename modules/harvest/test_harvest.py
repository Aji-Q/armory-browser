#!/usr/bin/env python3
"""harvest 自测 —— 不联网,只验提取层与引擎的判定逻辑

    python3 modules/harvest/test_harvest.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scout"))

import engine as eng       # noqa: E402
import extract as ex       # noqa: E402
import scout                # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" (期望 {want!r})"))
    if not ok:
        FAILED.append(name)


PAGE = """
<!doctype html><html lang="zh-CN"><head>
<title>测试页 — 商品目录</title>
<meta name="description" content="一个用于自测的页面">
<meta property="og:title" content="OG 标题">
<link rel="canonical" href="https://example.com/catalog">
<script>var tracking = 1;</script>
<style>.hidden{display:none}</style>
</head><body>
<div id="main" class="wrap">
  <h1 class="title">商品目录</h1>
  <p class="lead">这里有 <strong>3</strong> 件商品。</p>
  <ul class="items">
    <li class="item" data-id="1"><a href="/p/1">苹果</a> <span class="price">5 元</span></li>
    <li class="item" data-id="2"><a href="/p/2">香蕉</a> <span class="price">3 元</span></li>
    <li class="item" data-id="3"><a href="https://other.com/p/3">橙子</a> <span class="price">4 元</span></li>
  </ul>
  <table><tr><th>名称</th><th>价格</th></tr><tr><td>苹果</td><td>5</td></tr></table>
  <pre><code class="language-python">print("hi")</code></pre>
  <img src="/img/a.png" alt="示意图">
  <p>联系 <a href="mailto:x@y.z">我们</a> 或 <a href="#top">回顶部</a></p>
</div>
</body></html>
"""


def test_minidom():
    print("\n[MiniDOM] CSS 选择器")
    dom = ex.MiniDOM(PAGE)
    check("class 选择器", len(dom.select(".item")), 3)
    check("tag 选择器", len(dom.select("li")), 3)
    check("id 选择器", len(dom.select("#main")), 1)
    check("后代选择器", len(dom.select("ul.items li")), 3)
    check("直接子代", len(dom.select("ul > li")), 3)
    check("子代不越级", len(dom.select("div > li")), 0)
    check("属性存在", len(dom.select("[data-id]")), 3)
    check("属性等于", len(dom.select('[data-id="2"]')), 1)
    check("tag+class 组合", len(dom.select("li.item")), 3)
    check("逗号分组", len(dom.select("h1, .price")), 1 + 3)
    check("通配", len(dom.select("ul *")) > 0, True)
    check("不存在返回空", dom.select(".nope"), [])
    node = dom.select(".price")[0]
    check("取文本", node.text(), "5 元")
    check("取属性", dom.select("li")[0].attr("data-id"), "1")


def test_markdown():
    print("\n[html_to_markdown]")
    md = ex.html_to_markdown(PAGE)
    check("标题转 #", "# 商品目录" in md, True)
    check("粗体转 **", "**3**" in md, True)
    check("链接转 []()", "[苹果](/p/1)" in md, True)
    check("列表转 -", "- [苹果](/p/1)" in md, True)
    check("代码块带语言", "```python" in md, True)
    check("表格转管道", "| 名称 | 价格 |" in md, True)
    check("图片转 ![]()", "![示意图](/img/a.png)" in md, True)
    check("脚本内容不入正文", "tracking" not in md, True)
    check("样式内容不入正文", "display:none" not in md, True)
    check("不出现连续空行", "\n\n\n" not in md, True)


def test_text_and_meta():
    print("\n[文本与元数据]")
    text = ex.html_to_text(PAGE)
    check("文本含中文", "商品目录" in text, True)
    check("脚本被剥离", "tracking" not in text, True)
    meta = ex.extract_meta(PAGE)
    check("title", meta["title"], "测试页 — 商品目录")
    check("description", meta["description"], "一个用于自测的页面")
    check("og:title", meta["og:title"], "OG 标题")
    check("canonical", meta["canonical"], "https://example.com/catalog")
    check("lang", meta["lang"], "zh-CN")


def test_links_tables():
    print("\n[链接与表格]")
    links = ex.extract_links(PAGE, "https://example.com/catalog")
    hrefs = [l["href"] for l in links]
    check("相对链接绝对化", "https://example.com/p/1" in hrefs, True)
    check("外链保留", "https://other.com/p/3" in hrefs, True)
    check("mailto 被跳过", any(h.startswith("mailto:") for h in hrefs), False)
    check("锚点被跳过", "#top" in hrefs, False)
    check("去重", len(hrefs), len(set(hrefs)))
    check("外链标记", [l["external"] for l in links if l["href"] == "https://other.com/p/3"], [True])
    tables = ex.extract_tables(PAGE)
    check("表格行数", len(tables[0]), 2)
    check("表头", tables[0][0], ["名称", "价格"])


def test_regex():
    print("\n[apply_regex]")
    text = ex.html_to_text(PAGE)
    check("捕获组取值", ex.apply_regex(text, r"(\d+) 元"), ["5", "3", "4"])
    check("多行块", len(ex.apply_regex(PAGE, r"<li[^>]*>.*?</li>", 0)), 3)
    check("无命中返回空", ex.apply_regex(text, r"不存在的东西"), [])


def test_engine_units():
    print("\n[engine] 限速与 robots")
    limiter = eng.RateLimiter(rate=10)
    started = time.monotonic()
    for _ in range(3):
        limiter.acquire("https://a.example/x")
    elapsed = time.monotonic() - started
    check("同域 10rps 三次约 0.2s", 0.15 < elapsed < 0.45, True)

    started = time.monotonic()
    for i in range(3):
        limiter.acquire(f"https://h{i}.example/x")
    check("不同域名不互相排队", time.monotonic() - started < 0.1, True)

    gate = eng.RobotsGate(enabled=False)
    check("robots 关闭时放行", gate.allowed("https://x/y"), True)

    check("后端探测含 static", eng.available_backends()["static"], True)
    res = eng.Result(url="https://x", status=403)
    res.blocked = res.status in eng.BLOCKED_STATUS
    check("403 记为被拦", res.blocked, True)


def test_verdict_shared():
    print("\n[集成] scout 判定链被 harvest 复用")
    static_html = "<html><body><h1>标题</h1><p>" + "正文内容 " * 50 + "</p></body></html>"
    ev = scout.build_evidence(static_html, {"server": "nginx"})
    verdict, _, _, _ = scout.decide(ev)
    check("静态页 → 静态直取", verdict, "静态直取")

    js_html = ("<html><head><script>" + "x=1;" * 900 + "</script></head>"
               "<body><div id='app'></div><script>" + "render();" * 300 + "</script></body></html>")
    ev2 = scout.build_evidence(js_html, {})
    verdict2, _, _, _ = scout.decide(ev2)
    check("JS 页 → 动态渲染", verdict2, "动态渲染")

    cap_html = "<html><body><script src='https://www.google.com/recaptcha/api.js'></script></body></html>"
    ev3 = scout.build_evidence(cap_html, {"server": "cloudflare"})
    verdict3, _, _, _ = scout.decide(ev3)
    check("验证码 → 验证码闸门", verdict3, "验证码闸门")
    check("build_evidence 返回必要字段", "text_ratio" in ev and "cdn_challenge" in ev, True)


def test_auth_units():
    print("\n[auth] 登录状态判定与状态文件")
    import auth as auth_mod

    ok, _ = auth_mod.detect_login_state(
        "<a href='/logout'>Logout</a>", "https://x/login", "https://x/")
    check("含登出特征 → 成功", ok, True)
    ok, _ = auth_mod.detect_login_state(
        "<p>Invalid credentials</p>", "https://x/login", "https://x/login")
    check("含失败提示 → 失败", ok, False)
    ok, _ = auth_mod.detect_login_state(
        "<html></html>", "https://x/login", "https://x/dashboard")
    check("跳离登录页 → 成功", ok, True)
    ok, _ = auth_mod.detect_login_state(
        "<html></html>", "https://x/login", "https://x/login")
    check("停留登录页 → 未确认", ok, False)
    # 回归:登录成功页常带表单校验 JS,里面有裸 "invalid" 字样,不能因此判失败
    ok, _ = auth_mod.detect_login_state(
        "<a>Logout</a><script>var msg='invalid input';</script>", "https://x/login", "https://x/")
    check("成功页含 invalid 字样不误判", ok, True)
    ok, _ = auth_mod.detect_login_state(
        "<p>Invalid credentials</p><a>Logout</a>", "https://x/login", "https://x/")
    check("失败提示与登出特征并存 → 判失败", ok, False)
    ok, _ = auth_mod.detect_login_state(
        "<p>密码错误</p>", "https://x/login", "https://x/login")
    check("中文失败提示", ok, False)

    # 状态文件读写
    tmp = Path(tempfile.mkdtemp()) / "s.json"
    tmp.write_text(json.dumps({
        "cookies": [{"name": "session", "value": "abc"}, {"name": "uid", "value": "1"}],
        "_armory": {"login_url": "https://x/login", "saved_at": time.time() - 3600 * 30,
                    "ok": True, "reason": "test"}}), encoding="utf-8")
    cookies, meta = auth_mod.load_state(tmp)
    check("提取 cookie", cookies, {"session": "abc", "uid": "1"})
    check("计算登录龄", meta.get("age_hours"), 30.0)
    check("过期提醒", "超过 24 小时" in auth_mod.describe_state(tmp), True)


def test_captcha_module():
    print("\n[captcha] 验证码能力清单")
    import captcha as cap
    rows = cap.capability_report()
    kinds = {r[0]: r[1] for r in rows}
    check("清单有内容", len(rows) >= 5, True)
    check("列出图形验证码", "图形验证码" in kinds, True)
    check("图形码标为本地可用", kinds.get("图形验证码"), "本地可用")
    check("滑块如实标为不可靠", kinds.get("滑块验证"), "本地不可靠")
    check("reCAPTCHA 标为需平台", kinds.get("reCAPTCHA"), "需打码平台")
    check("置信度陷阱有警示", "不可作为筛选依据" in cap.confidence_warning(), True)
    if cap.available():
        # 1x1 白图:识别结果为空,只验证调用链通
        tiny = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c63000100000500010d0a2db400"
            "00000049454e44ae426082")
        out = cap.solve_text(tiny, charset="0123456789")
        check("solve_text 可调用(含字符集)", isinstance(out, str), True)
    else:
        print("  SKIP  ddddocr 未安装")


def test_auth_session_api():
    print("\n[auth] 登录态续期接口")
    import auth as auth_mod
    check("有探活函数", callable(getattr(auth_mod, "check_session", None)), True)
    check("有续期函数", callable(getattr(auth_mod, "ensure_valid", None)), True)
    # 关键一致性:登录与抓取必须用同一套浏览器引擎,否则一方找不到浏览器
    mod = auth_mod.playwright_like()
    check("登录引擎与引擎模块同源", mod in ("patchright", "playwright", None), True)
    if mod:
        import engine as eng
        check("两者选择的是同一个引擎", mod, eng.playwright_module())


def test_handoff_module():
    print("\n[handoff] 人工接管判定")
    import handoff as ho
    blocked, why = ho.need_human("<title>Security Check</title><h1>Verify you are human</h1>")
    check("识别人机验证页", blocked, True)
    check("给出原因", why, "人机验证")
    blocked, _ = ho.need_human("<title>首页</title><body><p>欢迎光临</p></body>")
    check("正常页不误判", blocked, False)
    # 关键:只在标题与开头判定 —— 讲验证码原理的文章正文里本来就有 captcha
    long_body = ("<title>爬虫教程</title><body>" + "正文内容" * 600
                 + "captcha 是验证码的意思</body>")
    blocked, _ = ho.need_human(long_body)
    check("正文深处提到 captcha 不误判", blocked, False)
    blocked, why = ho.need_human("<html></html>", status=403)
    check("403 判为需要人工", blocked, True)
    blocked, why = ho.need_human("<html></html>", status=429)
    check("429 判为需要人工", blocked, True)
    blocked, _ = ho.need_human("<title>扫码登录</title>")
    check("扫码登录页判为需要人工", blocked, True)
    check("引擎探测可调用", ho.playwright_like() in ("patchright", "playwright", None), True)
    found = ho.find_debug_browser(timeout=0.3)
    check("探测调试浏览器不抛异常", found is None or isinstance(found, dict), True)


def test_overlay_judgement():
    print("\n[handoff] 遮挡判定(判据是内容被没被扣,不是有没有弹窗)")
    import handoff as ho
    # 挂着登录弹窗但正文够长 —— 知乎热榜就是这种,不该判成需要人工
    with_content = ("<title>热榜</title><body>" + "正文内容" * 500
                    + "<div class='login-modal'>验证码登录 密码登录</div></body>")
    blocked, _ = ho.need_human(with_content)
    check("有弹窗有正文 → 不打扰人", blocked, False)
    # 纯登录页:只有浮层、没有正文 —— 这才是真被挡
    blocked, why = ho.need_human("<title>登录</title><body>验证码登录 密码登录</body>")
    check("纯登录页 → 需要人工", blocked, True)
    # 服务端扣内容的文案,比浮层信号硬
    blocked, why = ho.need_human("<title>文章</title><body>开头…<p>登录后查看全部</p></body>")
    check("登录墙文案 → 需要人工", blocked, True)
    check("原因标为登录墙", why, "登录墙")
    blocked, _ = ho.need_human("<title>文章</title><body>试读<p>订阅后继续阅读</p></body>")
    check("付费墙文案 → 需要人工", blocked, True)
    # 关键回归:内联 JS 里出现弹窗字样不算有弹窗(踩过的假阳性)
    js_page = ("<title>正常文章</title><body>" + "正文" * 800
               + "<script>var t='验证码登录';</script></body>")
    blocked, _ = ho.need_human(js_page)
    check("内联 JS 里的弹窗字样不误判", blocked, False)
    # 用户给了选择器时,命中与否是最硬的判据
    blocked, _ = ho.need_human("<title>x</title><body>y</body>", content_ok=True)
    check("css 命中 → 不打扰人", blocked, False)
    blocked, why = ho.need_human("<title>x</title><body>y</body>", content_ok=False)
    check("css 未命中 → 需要人工", blocked, True)
    check("可见文本量可计算", ho.visible_text_len("<p>abc</p><script>x=1</script>") >= 3, True)
    check("去脚本后不含 JS 字符串", "验证码登录" not in ho.strip_scripts(js_page), True)


def test_slider_locators():
    print("\n[captcha] 滑块定位方法")
    import captcha as cap
    for name in ("locate_slider", "locate_slider_opencv", "locate_slider_contour",
                 "locate_slider_rendered", "locate_slider_best"):
        check(f"有 {name}", callable(getattr(cap, name, None)), True)
    # 权重必须让实测最准的方法占优 —— 这是踩出来的:等权重会让差方法抢走选择权
    check("渲染模板权重最高", cap.METHOD_WEIGHT["rendered"] == max(cap.METHOD_WEIGHT.values()), True)
    check("ddddocr 权重最低", cap.METHOD_WEIGHT["ddddocr"] == min(cap.METHOD_WEIGHT.values()), True)
    # 传非法数据不该抛,而是返回 error
    r = cap.locate_slider_opencv(b"not-an-image", b"not-an-image")
    check("坏输入返回 error 而非抛异常", "error" in r, True)
    r = cap.locate_slider_rendered(b"not-an-image", b"not-an-image")
    check("渲染模板对坏输入也返回 error", "error" in r, True)


def test_identity_probe_units():
    """身份探针:端点推导 + 响应判定。不联网。"""
    import auth
    ip = auth.identity_probe_url
    check("根域 www.zhihu.com 命中", "zhihu.com/api/v4/me" in (ip("https://www.zhihu.com/hot") or ""), True)
    check("子域 mail.zhihu.com 认到根域表", ip("https://mail.zhihu.com/") == ip("https://zhihu.com/"), True)
    check("裸域 github.com 命中", "api.github.com" in (ip("https://github.com/") or ""), True)
    check("子域 gist.github.com 认到根域表", "api.github.com" in (ip("https://gist.github.com/") or ""), True)
    # 回归:曾经用 split(".", 1)[-1] 取后缀,"github.com" 退化成 "com",
    # 于是所有 .com 站点都去打 GitHub 的 /user,401 被误报成「登录态已失效」
    check("无表站点返回 None", ip("https://quotes.toscrape.com/"), None)
    check("example.com 不误命中", ip("https://example.com/"), None)
    check("notzhihu.com 不吃 zhihu.com 的后缀", ip("https://notzhihu.com/"), None)

    v = auth._identity_verdict
    check("身份接口 401 → 失效", v(401, "")[0], False)
    check("身份接口 403 → 失效", v(403, "")[0], False)
    check("身份接口 500 → 无法判定", v(500, "")[0], None)
    check("非 JSON 响应 → 无法判定", v(200, "<html>hi</html>")[0], None)
    check("isLogin:true → 有效", v(200, '{"data":{"isLogin":true}}')[0], True)
    check("isLogin:false → 失效", v(200, '{"data":{"isLogin":false}}')[0], False)
    check("返回身份字段 → 有效", v(200, '{"name":"u","id":"1"}')[0], True)
    check("响应含 error → 失效", v(200, '{"error":{"code":401}}')[0], False)


def test_review_fixes():
    """一轮代码审查中挖出来的坑,逐条钉住。不联网。"""
    import engine as eng

    # 未闭合的 script:被截断的 HTML 里 <script> 没有 </script>,少了 `|$` 兜底
    # 就会把整段脚本当正文 —— 虚高的字数直接喂给 1500 字升级阈值
    pad = "X" * 30
    closed = f'<body><script>var a="{pad}";</script><p>正文</p></body>'
    unclosed = f'<body><script>var a="{pad}";<p>正文</p></body>'
    check("闭合 script 只算正文", eng.visible_text_len(closed) < 10, True)
    check("未闭合 script 不虚高", eng.visible_text_len(unclosed) < 10, True)

    # 解码要看 Content-Type 的 charset:GBK 老站按 utf-8 解会整篇乱码,
    # 而 status 200、error 为空,看着完全成功
    check("声明 gbk 时按 gbk 解",
          eng.decode_body("中文测试".encode("gbk"), {"content-type": "text/html; charset=gbk"}),
          "中文测试")
    check("无 charset 声明退回 utf-8",
          eng.decode_body("中文".encode("utf-8"), {}), "中文")
    check("无效 charset 不抛异常",
          eng.decode_body(b"abc", {"content-type": "text/html; charset=no-such-charset"}), "abc")

    # embedded 曾经恒报可用,而引擎里根本没有它的实现 —— --check 会列出一个
    # 永远用不了的后端,按名字指定它则静默落回 auto
    check("available_backends 不再列 embedded",
          "embedded" in eng.available_backends(), False)
    check("available_backends 含 static",
          eng.available_backends().get("static"), True)

    # 坏 URL 必须变成 error 而不是冒泡:深爬的 pool.map 没有兜底,
    # 一个脏链接能中断整轮
    e = eng.Engine(rate=0, timeout=3)
    for bad in ("http://a:99999/", "http://a:abc/", "http://[::1/"):
        check(f"坏 URL 返回 error({bad[:16]})", bool(e.fetch_static(bad).error), True)


def test_slug_collision():
    """不同 URL 撞同一个 slug 会静默覆盖 —— 详见 harvest.save()。"""
    from harvest import slugify
    check("a.com 与 a.com/ 同 slug(故 save 必须去重)",
          slugify("https://a.com") == slugify("https://a.com/"), True)
    check("带查询串与不带同 slug(同上)",
          slugify("https://a.com/b?x=1") == slugify("https://a.com/b"), True)


def test_backend_memory_concurrency():
    """后端记忆在多线程下写盘不能互相踩。"""
    import tempfile, threading, json, os
    import memory as mem
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "m.json")
        m = mem.BackendMemory(path=p)
        def w(i):
            for j in range(12):
                m.record_fail(f"https://s{i}.com/", f"b{j % 3}", 200)
                m.record_ok(f"https://s{i}.com/", f"b{j % 3}")
        ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        data = json.load(open(p))
        check("并发写入后条目完整", len(data), 6)
        check("每条都有 ok_backend", all("ok_backend" in v for v in data.values()), True)
        check("没留下临时文件", [f for f in os.listdir(d) if f.endswith(".tmp")], [])


if __name__ == "__main__":
    print("harvest 自测 (不联网)")
    test_minidom()
    test_markdown()
    test_text_and_meta()
    test_links_tables()
    test_regex()
    test_engine_units()
    test_verdict_shared()
    test_auth_units()
    test_captcha_module()
    test_auth_session_api()
    test_handoff_module()
    test_overlay_judgement()
    test_slider_locators()
    test_identity_probe_units()
    test_review_fixes()
    test_slug_collision()
    test_backend_memory_concurrency()
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {', '.join(FAILED)}")
        sys.exit(1)
    print("全部通过")
