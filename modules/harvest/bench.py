#!/usr/bin/env python3
"""bench —— 本地回环压测:量出工具本身的吞吐上限

只打 127.0.0.1。打真实站点测出来的不是工具的极限,而是对方的限流阈值,
而且那是在拿别人的服务器做压力测试。

测三件事:
  1. 不同并发下的 QPS —— 找拐点,过拐点后加线程只会互相抢 GIL
  2. keep-alive 开/关的差异 —— 连接复用的实际收益
  3. HTTP vs HTTPS —— TLS 握手开销省掉多少

用法:
    python3 modules/harvest/bench.py
    python3 modules/harvest/bench.py --requests 400 --concurrency 1,4,16,64,128
    python3 modules/harvest/bench.py --tls
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.server
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine as eng  # noqa: E402

# 20KB 页面 —— 空响应测出来的是调度开销,不是真实负载
PAGE = ("<html><head><title>bench</title></head><body>"
        + "<div class='row'><span class='cell'>payload</span></div>" * 400
        + "</body></html>").encode()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # 关键:1.0 默认不支持 keep-alive
    delay = 0.0                            # 人为延迟,模拟真实网络 RTT

    def do_GET(self):
        if self.delay:
            time.sleep(self.delay)
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /private\n"
        else:
            body = PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    # socketserver 默认 backlog 只有 5。TLS 握手比明文慢得多,并发建连时
    # accept 队列会溢出,表现为高并发下 QPS 断崖 —— 那是压测服务端的锅,不是客户端的
    request_queue_size = 512


def make_cert(tmp):
    """自签证书,只为本机压测用。"""
    cert, key = Path(tmp) / "c.pem", Path(tmp) / "k.pem"
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", str(key), "-out", str(cert), "-days", "1",
           "-subj", "/CN=localhost",
           "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        return None, None
    return cert, key


def run_group(url_fn, count, concurrency, keepalive, rotate_ua=False, warmup=0):
    """跑一组:返回 (qps, 总耗时, 新建连接数, 复用次数, 错误数)。

    warmup > 0 时先按同样并发空跑一轮填满连接池,把握手成本排除在测量之外。
    """
    engine = eng.Engine(rate=0, keepalive=keepalive, rotate_ua=rotate_ua,
                        retries=0, timeout=20)
    urls = [url_fn(i) for i in range(count)]

    def one(url):
        res = engine.fetch_static(url)
        return 1 if res.error else 0

    if warmup:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(one, [url_fn(i) for i in range(warmup)]))
    base_opened, base_reused = engine.conn_pool.opened, engine.conn_pool.reused

    errors = 0
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for err in pool.map(one, urls):
            errors += err
    elapsed = time.monotonic() - started
    opened = engine.conn_pool.opened - base_opened
    reused = engine.conn_pool.reused - base_reused
    engine.conn_pool.close_all()
    qps = count / elapsed if elapsed else 0.0
    return qps, elapsed, opened, reused, errors


def report(rows, title):
    print(f"\n{title}")
    print(f"{'并发':>6} {'QPS':>10} {'总耗时':>9} {'新建连接':>9} {'复用率':>8} {'错误':>5}")
    print("-" * 54)
    for c, qps, elapsed, opened, reused, errors in rows:
        total = opened + reused
        rate = (reused / total * 100) if total else 0.0
        print(f"{c:>6} {qps:>10.1f} {elapsed:>8.2f}s {opened:>9} {rate:>7.1f}% {errors:>5}")


def main():
    ap = argparse.ArgumentParser(description="harvest 本地压测")
    ap.add_argument("--requests", type=int, default=1000, help="每组总请求数")
    ap.add_argument("--repeat", type=int, default=3, help="每组重复次数,取最优(测上限)")
    ap.add_argument("--concurrency", default="1,4,16,32,64,128", help="并发梯度")
    ap.add_argument("--tls", action="store_true", help="改用自签 HTTPS 测 TLS 开销")
    ap.add_argument("--rotate-ua", action="store_true", help="同时开启 UA 轮换")
    ap.add_argument("--warmup", type=int, default=0, help="测量前预热请求数,填满连接池")
    ap.add_argument("--delay", type=int, default=0, help="服务端人为延迟(毫秒),模拟真实 RTT")
    args = ap.parse_args()

    Handler.delay = args.delay / 1000.0

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]

    httpd = Server(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    scheme, tmpdir = "http", None
    if args.tls:
        tmpdir = tempfile.TemporaryDirectory()
        cert, key = make_cert(tmpdir.name)
        if not cert:
            print("[!] openssl 不可用,无法生成自签证书", file=sys.stderr)
            return 1
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"{scheme}://127.0.0.1:{port}"
    print(f"本地 {scheme.upper()} 服务已起于 {base},页面 {len(PAGE) / 1024:.0f} KB,"
          f"每组 {args.requests} 个请求 × 重复 {args.repeat} 次取最优")

    def sweep(keepalive, use_levels=None, use_requests=None):
        use_levels = use_levels or levels
        use_requests = use_requests or args.requests
        rows = []
        for c in use_levels:
            best = None
            for _ in range(max(args.repeat, 1)):
                got = run_group(lambda i: f"{base}/page/{i}", use_requests, c,
                                keepalive=keepalive, rotate_ua=args.rotate_ua,
                                warmup=args.warmup)
                if best is None or got[0] > best[0]:
                    best = got
            rows.append((c, *best))
            print(f"  并发 {c:<4} → {best[0]:7.1f} QPS", file=sys.stderr)
        return rows

    rows = sweep(True)
    report(rows, f"[keep-alive 开] {scheme.upper()} @ 127.0.0.1"
                 + ("  + UA 轮换" if args.rotate_ua else ""))
    peak = max(rows, key=lambda r: r[1])
    print(f"\n峰值 {peak[1]:.1f} QPS @ 并发 {peak[0]}")
    last = rows[-1]
    if last[0] != peak[0] and last[1] < peak[1] * 0.85:
        print(f"拐点:并发 {peak[0]} 之后开始下降,{last[0]} 并发只剩 {last[1]:.0f} QPS"
              f"({last[1] / peak[1] * 100:.0f}%) —— 瓶颈已不在网络,是本地 CPU 与 GIL 在互抢")

    # 关掉复用必然每次都建连:HTTPS 下每次建连都要付 TLS 握手,样本数调小免得等
    off_levels = [c for c in levels if c <= 64] or levels[:3]
    off_requests = min(args.requests, 300)
    off = sweep(False, off_levels, off_requests)
    report(off, f"[keep-alive 关] 同样负载(该路径走 urllib,连接统计不适用)"
                f",每组 {off_requests} 请求")
    off_peak = max(off, key=lambda r: r[1])
    if off_peak[1]:
        print(f"\n连接复用收益: {peak[1] / off_peak[1]:.2f}× "
              f"({off_peak[1]:.0f} → {peak[1]:.0f} QPS)")

    httpd.shutdown()
    httpd.server_close()
    if tmpdir:
        tmpdir.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
