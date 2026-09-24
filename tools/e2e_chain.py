#!/usr/bin/env python3
"""e2e_chain —— 全链路验证:scout 判定 → signer 复现签名 → harvest 带签名直连

场景:目标站点的接口要求一个签名参数,签名算法在前端 JS 里。链路是——

    1. scout  发现接口线索 + 混淆脚本 → 判定「疑似参数签名」
    2. signer 在补环境沙箱里跑目标 JS,复现出签名
    3. harvest 带上签名直连接口,拿到数据

用一个本地 mock 站点做可控验证:服务端和前端 JS 实现同一个签名算法,
签名对就给数据,不对就 403。这样能确认「signer 算出来的东西真的能用」,
而不是只确认「函数没报错」。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "modules" / "harvest"))
sys.path.insert(0, str(ROOT / "modules" / "scout"))
sys.path.insert(0, str(ROOT / "modules" / "signer"))

import engine as eng          # noqa: E402
import extract as ex          # noqa: E402
import scout as scout_mod     # noqa: E402
import signer                 # noqa: E402

SALT = "n%A-rKaT5fb[Gy?;N5@Tj"       # 故意用素材里那种形态的盐值

# 前端 JS:签名算法。和 Python 侧必须完全一致,否则验证没有意义
FRONTEND_JS = """
function js_sign(text) {
    var s = text + "%s";
    var h = 0;
    for (var i = 0; i < s.length; i++) {
        h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    }
    return Math.abs(h).toString(36) + "-" + s.length;
}

// 页面里常见的调用形态:从 URL 取参数再签
function signPage(path) {
    return js_sign(path);
}
""" % SALT


def py_sign(text):
    """服务端实现:必须与上面的 JS 等价。"""
    s = text + SALT
    h = 0
    for ch in s:
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    # JS 的 Math.abs().toString(36)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    n, out = abs(h), ""
    while n:
        out = digits[n % 36] + out
        n //= 36
    return (out or "0") + "-" + str(len(s))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/app.js":
            body = FRONTEND_JS.encode()
            self._send(200, body, "application/javascript")
            return

        if parsed.path == "/api/data":
            got = (query.get("sign") or [""])[0]
            want = py_sign("/api/data?q=" + (query.get("q") or [""])[0])
            if got != want:
                self._send(403, b"signature rejected", "text/plain")
                return
            payload = {"ok": True, "q": query.get("q", [""])[0],
                       "items": [{"id": i, "name": f"item-{i}"} for i in range(1, 4)]}
            self._send(200, json.dumps(payload).encode(), "application/json")
            return

        # 首页:内嵌加密脚本 + 接口线索,让 scout 能判出签名层
        html = (f"<html><head><title>sig demo</title>"
                f"<script src='/app.js'></script>"
                f"<script>var api='/api/data'; var salt='{SALT}';"
                f"fetch('/api/data?q=x&sign='+signPage('/api/data?q=x'));</script>"
                f"</head><body><h1>签名接口演示</h1>"
                f"<p>本页数据需签名参数。</p></body></html>").encode()
        self._send(200, html, "text/html")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    srv = None
    started = False
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.request_queue_size = 64
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started = True
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        return run_chain(base)
    except Exception as exc:
        print(f"全链路异常: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if srv is not None:
            try:
                # serve_forever 没成功启动时调用 shutdown 会永久等待。
                if started:
                    srv.shutdown()
            finally:
                srv.server_close()


def run_chain(base):
    ok = True

    print("=" * 70)
    print("全链路验证:scout 判定 → signer 复现签名 → harvest 带签名直连")
    print("=" * 70)

    # ---------- 1. scout 判定
    print("\n[1] scout:侦察目标,看能否判出签名层")
    e = eng.Engine(rate=0, timeout=10, retries=0, prefer_cffi=False, keepalive=False)
    res = e.fetch_static(base + "/")
    ev = scout_mod.build_evidence(res.html, res.headers)
    verdict, why, layers, _ = scout_mod.decide(ev)
    print(f"    判定: {verdict}")
    print(f"    依据: {why}")
    print(f"    接口线索: {ev['api_hints'][:3]}")
    sign_layer = layers.get("签名层", {})
    print(f"    签名层: {sign_layer.get('level')} — {sign_layer.get('evidence')}")
    found_salt = any(SALT[:10] in s for s, _ in signer.find_salts(res.html))
    print(f"    盐值识别: {'命中' if found_salt else '未命中'}")
    if verdict != "疑似参数签名" and sign_layer.get("level") != "疑似参数签名":
        print("    ! 未判出签名层(接口线索与混淆脚本需同时命中)")
        ok = False

    # ---------- 2. signer 复现签名
    print("\n[2] signer:在补环境沙箱里跑前端 JS")
    target_path = "/api/data?q=hello"
    with tempfile.TemporaryDirectory(prefix="armory-chain-") as tempdir:
        js_file = Path(tempdir) / "sig.js"
        js_file.write_text(FRONTEND_JS, encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "modules" / "signer" / "signer.py"),
                            "run", str(js_file), "signPage", "--args", json.dumps([target_path])],
                           capture_output=True, text=True, timeout=90)
    # 退出码必须查:signer 崩溃时若 stdout 恰好留了一行,签名比对可能侥幸通过,
    # 于是整条链路被记成成功 —— 而实际上这一步根本没跑起来
    if r.returncode != 0:
        print(f"    ! signer 退出码 {r.returncode}: {(r.stderr or '')[:140]}")
        ok = False
    js_sig = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
    expected = py_sign(target_path)
    print(f"    signer 输出: {js_sig}")
    print(f"    服务端期望:  {expected}")
    if js_sig != expected:
        print(f"    ! 签名不一致(stderr: {(r.stderr or '')[:120]})")
        ok = False
    else:
        print("    签名一致 ✓")

    # ---------- 3. harvest 带签名直连
    print("\n[3] harvest:带上签名请求接口")
    unsigned = e.fetch_static(f"{base}{target_path}")
    print(f"    不带签名 → HTTP {unsigned.status}  "
          f"{unsigned.html[:40]}")
    signed = e.fetch_static(f"{base}{target_path}&sign={js_sig}")
    print(f"    带签名   → HTTP {signed.status}")
    if signed.status == 200:
        try:
            data = json.loads(signed.html)
            if not isinstance(data, dict) or data.get("ok") is not True or data.get("q") != "hello":
                raise ValueError("业务响应必须包含 ok=true、q=hello")
            items = data.get("items")
            if not isinstance(items, list) or len(items) != 3 or not all(
                    isinstance(item, dict) and set(item) == {"id", "name"}
                    and type(item["id"]) is int and item["id"] == index
                    and type(item["name"]) is str and item["name"] == f"item-{index}"
                    for index, item in enumerate(items, 1)):
                raise ValueError("items 必须为 id=1..3/name=item-1..3 的三个对象")
            print(f"    拿到数据: {len(items)} 条  {items[:1]}")
        except (ValueError, TypeError) as exc:
            # 200 但不是 JSON,说明「带签名拿到数据」并不成立 —— 很可能只是
            # 打到了某个 HTML 页面。仅打印不判失败会让整条链路被记成通过。
            print(f"    ! 200 但 JSON/schema/值无效: {exc}; 响应: {(signed.html or '')[:80]}")
            ok = False
    if unsigned.error or signed.error or unsigned.status != 403 or signed.status != 200:
        print("    ! 接口行为不符预期(应为 403 → 200)")
        ok = False

    print("\n" + "=" * 70)
    print("全链路结果:", "通过 ✓" if ok else "未通过 ✗")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
