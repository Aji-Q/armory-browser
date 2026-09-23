#!/usr/bin/env python3
"""signer —— JS 签名复现器

签名逆向的最后一步:把从站点扒下来的 JS 跑起来,复现出它生成的参数。
难点从来不是算法,是**补环境** —— 站点 JS 假设自己跑在浏览器里,
会访问 navigator / document / window 的几十个属性,缺一个就 ReferenceError。

做法:不逐个猜 API,而是用 node 的 vm 建一个 Proxy 沙箱。
未知属性一律返回可链式访问的替身对象,并把访问过的名字记下来,
执行完打印给你看 —— 补环境从「猜」变成「看」。

用法:
    python3 modules/signer/signer.py probe target.js          # 看有哪些函数可调、用了什么算法
    python3 modules/signer/signer.py run target.js sign       # 调用 sign()
    python3 modules/signer/signer.py run target.js sign --args '["a", 1]'
    python3 modules/signer/signer.py run target.js --raw 'sign("x")'   # 直接执行表达式

注意:站点 JS 在 node 里拥有完整权限(可读写文件)。只在分析环境里跑。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------- 运行器

RUNNER = r"""
// armory signer runner —— vm + Proxy 沙箱,未知环境属性自动兜底
'use strict';
const vm = require('vm');
const fs = require('fs');

const targetFile = process.argv[2];
const spec = JSON.parse(process.argv[3] || '{}');
const timeoutMs = spec.timeout || 15000;
const code = fs.readFileSync(targetFile, 'utf8');

const missing = new Set();
const cache = new Map();

// 替身的属性访问规则。符号属性最容易踩:返回 undefined 的话,
// 代码里 for...of 或 await 一碰就崩,必须给可调用的替身。
function stubGet(path, k) {
  if (k === Symbol.toPrimitive) return () => path;
  if (k === Symbol.iterator) return function () {
    return { next: () => ({ done: true, value: undefined }) };
  };
  if (k === Symbol.asyncIterator) return function () {
    return { next: async () => ({ done: true, value: undefined }) };
  };
  if (k === Symbol.toStringTag) return 'Object';
  if (k === 'then') return (res) => { try { if (res) res(makeStub(path)); } catch (e) {} };
  if (k === 'toString') return () => path;
  if (k === 'valueOf') return () => 0;
  if (k === 'length') return 0;
  if (typeof k === 'symbol') return undefined;
  return makeStub(path + '.' + String(k));
}

function makeStub(path) {
  if (cache.has(path)) return cache.get(path);
  const p = new Proxy(function () {}, {
    get(t, k) {
      // 标记:调用前要靠它区分「JS 里真有的函数」和「兜底的替身」
      if (k === '__armory_stub__') return true;
      return stubGet(path, k);
    },
    set() { return true; },
    apply() { return makeStub(path + '()'); },
    construct() { return makeStub('new ' + path); },
    has() { return true; },
  });
  cache.set(path, p);
  return p;
}

// 已知属性用真实值,未知属性回落到替身并记账
function wrap(obj, path) {
  return new Proxy(obj, {
    get(t, k) {
      if (k in t) return t[k];
      missing.add(path + '.' + String(k));
      return stubGet(path, k);
    },
    has() { return true; },
    set(t, k, v) { t[k] = v; return true; },
  });
}

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
           '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36';

const store = () => {
  const m = new Map();
  return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)),
           removeItem: k => m.delete(k), clear: () => m.clear(), key: () => null,
           get length() { return m.size; } };
};

// has:true 的陷阱会把 context 自带的内置对象一并遮蔽,漏写一个就是 ReferenceError。
// 所以先从 node 全局继承全部内置,再用浏览器侧的模拟覆盖上去。
const inherited = {};
for (const k of Object.getOwnPropertyNames(globalThis)) {
  if (['process', 'require', 'module', 'exports', 'global', 'globalThis',
       'window', 'self', 'top', 'parent', 'frames'].includes(k)) continue;
  try { inherited[k] = globalThis[k]; } catch (e) {}
}

const sandboxTarget = Object.assign(inherited, {
  console, JSON, Math, Date, String, Number, Boolean, Array, Object, RegExp, Error,
  TypeError, RangeError, Symbol, Map, Set, WeakMap, WeakSet, Promise, Proxy, Reflect,
  Function, ArrayBuffer, Uint8Array, Int8Array, Uint16Array, Int16Array, Uint32Array,
  Int32Array, Float32Array, Float64Array, DataView, BigInt, BigInt64Array, BigUint64Array,
  encodeURIComponent, decodeURIComponent, encodeURI, decodeURI, escape, unescape,
  parseInt, parseFloat, isNaN, isFinite, undefined: undefined,
  setTimeout: (fn) => { try { return fn(); } catch (e) { return undefined; } },
  setInterval: () => 0, clearInterval: () => {}, clearTimeout: () => {},
  atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
  btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
  TextEncoder: require('util').TextEncoder,
  TextDecoder: require('util').TextDecoder,
  Buffer,
  process: { argv: [], env: {}, version: 'v18.0.0', platform: 'win32' },
  performance: { now: () => Date.now(), timeOrigin: Date.now() },
  crypto: {
    getRandomValues: (arr) => { for (let i = 0; i < arr.length; i++) arr[i] = (Math.random() * 256) | 0; return arr; },
    randomUUID: () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c =>
      ((Math.random() * 16) | 0).toString(16)),
  },
  navigator: wrap({
    userAgent: UA, appVersion: UA.replace('Mozilla/', ''), appName: 'Netscape',
    platform: 'Win32', language: 'zh-CN', languages: ['zh-CN', 'zh', 'en'],
    cookieEnabled: true, webdriver: false, hardwareConcurrency: 8, deviceMemory: 8,
    maxTouchPoints: 0, vendor: 'Google Inc.', onLine: true, doNotTrack: null,
    plugins: wrap({ length: 0 }, 'navigator.plugins'),
    mimeTypes: wrap({ length: 0 }, 'navigator.mimeTypes'),
    userAgentData: { brands: [], mobile: false, platform: 'Windows' },
  }, 'navigator'),
  screen: wrap({ width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
                 colorDepth: 24, pixelDepth: 24 }, 'screen'),
  location: wrap({
    href: 'https://example.com/', protocol: 'https:', host: 'example.com',
    hostname: 'example.com', port: '', pathname: '/', search: '', hash: '',
    origin: 'https://example.com', toString: () => 'https://example.com/',
  }, 'location'),
  history: wrap({ length: 1, pushState: () => {}, replaceState: () => {}, back: () => {},
                  forward: () => {} }, 'history'),
  localStorage: wrap(store(), 'localStorage'),
  sessionStorage: wrap(store(), 'sessionStorage'),
  document: wrap({
    cookie: '', title: '', referrer: 'https://www.google.com/', readyState: 'complete',
    characterSet: 'UTF-8', compatMode: 'CSS1Compat', hidden: false, visibilityState: 'visible',
    documentElement: { style: {}, clientWidth: 1920, clientHeight: 1080 },
    body: { style: {}, appendChild: () => {}, removeChild: () => {} },
    head: { appendChild: () => {}, removeChild: () => {} },
    createElement: () => wrap({ style: {}, setAttribute: () => {}, getAttribute: () => null,
                                appendChild: () => {}, getContext: () => null,
                                toDataURL: () => '' }, 'element'),
    createTextNode: () => wrap({}, 'textNode'),
    // 真实环境找不到元素返回 null,但补环境时要让代码继续跑 —— 返回替身
    getElementById: () => makeStub('element'),
    getElementsByTagName: () => [], getElementsByClassName: () => [],
    querySelector: () => makeStub('element'), querySelectorAll: () => [],
    addEventListener: () => {}, removeEventListener: () => {},
    write: () => {}, writeln: () => {},
  }, 'document'),
  // CommonJS 外壳:有些站点 JS 用 module.exports / exports 组织代码。
  // 必须套 wrap —— 返回裸对象的化,代码里 .xxx.yyy 一碰就 undefined 报错
  exports: wrap({}, 'exports'),
  module: wrap({ exports: {} }, 'module'),
  require: () => makeStub('require()'),
  global: null,                                      // 稍后指向 context 的 global
});

// 陷阱必须挂在传给 createContext 的那个对象上 —— 第二个参数是 contextObject,
// 不是陷阱配置,传错位置等于没有兜底
const sandbox = new Proxy(sandboxTarget, {
  has: () => true,                                   // 让所有裸变量查找都命中沙箱
  get(t, k) {
    if (k in t) return t[k];
    if (typeof k === 'symbol') return undefined;
    missing.add(String(k));
    return makeStub(String(k));
  },
  set(t, k, v) { t[k] = v; return true; },
  deleteProperty(t, k) { delete t[k]; return true; },
});
const ctx = vm.createContext(sandbox);

// window 必须指向 context 里的 global 本身,而不是裸 sandbox 对象 ——
// 指向裸对象的话 window.未知属性 会拿到 undefined,再往下点一层就 TypeError
vm.runInContext(
  'window = this; self = this; top = this; parent = this; frames = this; global = this; ' +
  'try { globalThis = this; } catch (e) {}', ctx);

let error = null, errContext = null;

// 把出错行号和该行源码抠出来 —— 混淆代码光有报错信息根本定位不到位置
function locate(err) {
  const stack = (err && err.stack) ? String(err.stack) : '';
  const m = /target\.js:(\d+)/.exec(stack);
  if (!m) return null;
  const ln = parseInt(m[1], 10);
  const lines = code.split('\n');
  const from = Math.max(0, ln - 2);
  return lines.slice(from, ln + 1)
    .map((t, i) => String(from + i + 1).padStart(5) + ' | ' + t.trim().slice(0, 130))
    .join('\n');
}

try {
  vm.runInContext(code, ctx, { timeout: timeoutMs, filename: 'target.js' });
} catch (e) {
  error = (e && e.message) ? e.message : String(e);
  errContext = locate(e);
}

function report(payload) {
  process.stdout.write('__ARMORY_JSON__' + JSON.stringify(payload) + '\n');
}

if (error) {
  report({ ok: false, stage: 'load', error, context: errContext,
           missing: Array.from(missing).slice(0, 60) });
  process.exit(0);
}

let result, callError = null;
try {
  if (spec.raw) {
    result = vm.runInContext(spec.raw, ctx, { timeout: timeoutMs, filename: 'call' });
  } else if (spec.func) {
    const fn = sandbox[spec.func];
    if (typeof fn !== 'function' || fn.__armory_stub__) {
      report({ ok: false, stage: 'call',
               error: 'not a defined function: ' + spec.func +
                      (fn && fn.__armory_stub__
                        ? ' —— 命中的是替身,这个函数在目标 JS 里并不存在' : ''),
               callables: listCallables(), missing: Array.from(missing).slice(0, 60) });
      process.exit(0);
    }
    result = fn.apply(null, spec.args || []);
  }
} catch (e) {
  callError = (e && e.message) ? e.message : String(e);
}

function listCallables() {
  const out = [];
  for (const k of Object.keys(sandbox)) {
    try {
      const v = sandbox[k];
      if (typeof v === 'function' && !v.__armory_stub__) out.push(k);
    } catch (e) {}
  }
  return out;
}

report({
  ok: callError === null,
  result: result === undefined ? null : result,
  error: callError,
  missing: Array.from(missing).slice(0, 60),
  callables: listCallables(),
});
"""


def find_node():
    return shutil.which("node") or shutil.which("nodejs")


def run_js(js_path, func=None, args=None, raw=None, timeout=15, node=None):
    node = node or find_node()
    if not node:
        raise RuntimeError("需要 node 运行时(node 或 nodejs),未找到")
    spec = {"timeout": timeout * 1000}
    if raw:
        spec["raw"] = raw
    elif func:
        spec["func"], spec["args"] = func, args or []
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "runner.js"
        runner.write_text(RUNNER, encoding="utf-8")
        proc = subprocess.run([node, str(runner), str(js_path), json.dumps(spec)],
                              capture_output=True, text=True, timeout=timeout + 15)
    out = proc.stdout
    marker = "__ARMORY_JSON__"
    if marker in out:
        payload = json.loads(out.split(marker, 1)[1].splitlines()[0])
        payload["stderr"] = proc.stderr.strip()[:800] if proc.stderr else ""
        return payload
    return {"ok": False, "stage": "runner",
            "error": (proc.stderr or out or "node 无输出").strip()[:800]}


# ---------------------------------------------------------------- 静态分析

CRYPTO_SIGNATURES = {
    "MD5": [
        r"\bmd5\b", r"CryptoJS\.MD5", r"hex_md5", r"hexMd5",
        r"0x67452301", r"1732584193", r"-271733879", r"271733878",
        r"65535\s*&\s*\w",                       # MD5 的 add32 实现特征
    ],
    "SHA1": [r"\bsha1\b", r"hex_sha1", r"0x67452301", r"sha-1"],
    "SHA256": [r"\bsha256\b", r"CryptoJS\.SHA256", r"0x6a09e667", r"1779033703"],
    "HMAC": [r"\bhmac\b", r"HmacSHA\d"],
    "AES": [r"\baes\b", r"AES\.encrypt", r"rijndael", r"\bCBC\b", r"\bECB\b", r"createCipheriv"],
    "DES/3DES": [r"\bdes\b", r"tripledes", r"3des", r"createDecipheriv"],
    "RSA": [r"\brsa\b", r"RSAKey", r"setPublic", r"JSEncrypt", r"pkcs1", r"setPublicKey"],
    "Base64": [r"\bbtoa\b", r"base64", r"fromCharCode"],
    "CryptoJS": [r"CryptoJS"],
    "自定义混淆": [r"_0x[0-9a-f]{4,}", r"\\x[0-9a-f]{2}\\x", r"eval\s*\(\s*function"],
}

# 可疑的硬编码盐值:长随机串,且不是常见的占位
SALT_RE = re.compile(r"""["']([A-Za-z0-9!@#$%^&*()_+\-=\[\]{};:'",.<>/?|`~]{12,64})["']""")
SALT_STOPWORDS = ("application/json", "text/html", "text/plain", "utf-8", "UTF-8",
                  "application/x-www-form-urlencoded", "Mozilla/5.0", "www.w3.org")
# 签名常见的参数名
SIGN_PARAM_RE = re.compile(
    r"""["'](x[-_]?sign|sign|signature|_sign|token|_token|nonce|timestamp|ts|salt|"""
    r"""x[-_]?zse[-_]?\d+|x[-_]?s|x[-_]?t|access[-_]?key|app[-_]?key)["']""", re.I)


def detect_crypto(js_code):
    hits = {}
    for algo, patterns in CRYPTO_SIGNATURES.items():
        for p in patterns:
            if re.search(p, js_code, re.I):
                hits.setdefault(algo, 0)
                hits[algo] += len(re.findall(p, js_code, re.I))
    return hits


def find_salts(js_code, limit=12):
    """挑可疑盐值:长、无空格、不像 URL 或 MIME。"""
    counts = {}
    for m in SALT_RE.finditer(js_code):
        s = m.group(1)
        if any(w in s for w in SALT_STOPWORDS):
            continue
        if s.startswith(("http", "//", "./", "/")):
            continue
        if re.fullmatch(r"[0-9a-fA-F]{32,}", s):        # 纯十六进制,更像内置常量
            continue
        if len(set(s)) < 5:                              # aaaaaa 这种
            continue
        counts[s] = counts.get(s, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def list_functions(js_code):
    """静态扫顶层函数名与 var xxx = function 形式。"""
    names = set(re.findall(r"^\s*function\s+([A-Za-z_$][\w$]*)\s*\(", js_code, re.M))
    names |= set(re.findall(r"^\s*(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*function", js_code, re.M))
    names |= set(re.findall(r"^\s*(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*\(?[\w,\s]*\)?\s*=>", js_code, re.M))
    names |= set(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*=\s*function", js_code, re.M))
    return sorted(names)


def find_sign_params(js_code):
    return sorted({m.group(1) for m in SIGN_PARAM_RE.finditer(js_code)})


def find_env_touches(js_code):
    """JS 直接碰了哪些浏览器对象 —— 决定补环境要补什么。"""
    objs = ["window", "document", "navigator", "location", "screen", "history",
            "localStorage", "sessionStorage", "crypto", "performance", "XMLHttpRequest",
            "fetch", "WebSocket", "Worker", "canvas", "WebGLRenderingContext"]
    out = {}
    for o in objs:
        n = len(re.findall(r"\b" + o + r"\b", js_code))
        if n:
            out[o] = n
    return out


# ---------------------------------------------------------------- CLI

def cmd_probe(args):
    path = Path(args.js)
    code = path.read_text(encoding="utf-8", errors="replace")
    print(f"文件: {path}({len(code):,} 字符,{code.count(chr(10)) + 1} 行)\n")

    funcs = list_functions(code)
    print(f"候选函数({len(funcs)} 个):")
    for f in funcs[:40]:
        print(f"  {f}")
    if len(funcs) > 40:
        print(f"  …另有 {len(funcs) - 40} 个")

    print()
    crypto = detect_crypto(code)
    if crypto:
        print("算法特征:")
        for algo, n in sorted(crypto.items(), key=lambda kv: -kv[1]):
            print(f"  {algo:<10} {n} 处")
    else:
        print("算法特征: 未识别到已知算法")

    salts = find_salts(code)
    if salts:
        print("\n可疑盐值/常量:")
        for s, n in salts:
            print(f"  {s!r}  ×{n}")

    params = find_sign_params(code)
    if params:
        print(f"\n签名相关参数名: {', '.join(params[:20])}")

    env = find_env_touches(code)
    if env:
        print("\n碰到的浏览器对象:" if env else "")
        print("  " + "  ".join(f"{k}({v})" for k, v in sorted(env.items(), key=lambda kv: -kv[1])))

    print(f"\n下一步: python3 {sys.argv[0]} run {path} <函数名>")
    return 0


def cmd_run(args):
    path = Path(args.js)
    if not path.is_file():
        print(f"[!] 文件不存在: {path}", file=sys.stderr)
        return 1
    result = run_js(path, func=args.func, args=args.args, raw=args.raw,
                    timeout=args.timeout, node=args.node)

    if result.get("ok"):
        value = result.get("result")
        if isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            print(value)
    else:
        stage = result.get("stage", "?")
        print(f"[!] 失败({stage}): {result.get('error')}", file=sys.stderr)
        if result.get("context"):
            print("\n出错位置:", file=sys.stderr)
            for line in result["context"].splitlines():
                print(f"    {line}", file=sys.stderr)
        if result.get("callables"):
            print(f"\n    沙箱里可调用的函数: {', '.join(result['callables'][:30])}", file=sys.stderr)

    missing = result.get("missing") or []
    if missing:
        print(f"\n[i] 运行期间访问了 {len(missing)} 个环境属性(已用替身兜住):", file=sys.stderr)
        for m in missing[:24]:
            print(f"      {m}", file=sys.stderr)
        if len(missing) > 24:
            print(f"      …另有 {len(missing) - 24} 个", file=sys.stderr)
        print("    结果若不对,从这些名字入手补真实值", file=sys.stderr)

    return 0 if result.get("ok") else 1


def main():
    ap = argparse.ArgumentParser(description="JS 签名复现器 —— 补环境 + 执行")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("probe", help="静态分析:候选函数 / 算法 / 盐值 / 环境依赖")
    p1.add_argument("js")
    p1.set_defaults(handler=cmd_probe)

    p2 = sub.add_parser("run", help="在补环境沙箱里执行")
    p2.add_argument("js")
    p2.add_argument("func", nargs="?", help="要调用的函数名")
    p2.add_argument("--args", help='JSON 数组形式的参数,如 \'["keyword",1]\'')
    p2.add_argument("--raw", help='直接执行表达式,如 \'sign("x")\'')
    p2.add_argument("--timeout", type=int, default=15)
    p2.add_argument("--node", help="指定 node 路径")
    p2.set_defaults(handler=cmd_run)

    args = ap.parse_args()
    if getattr(args, "args", None):
        try:
            args.args = json.loads(args.args)
        except json.JSONDecodeError as exc:
            print(f"[!] --args 不是合法 JSON: {exc}", file=sys.stderr)
            return 1
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
