#!/usr/bin/env python3
"""signer 自测 —— 静态分析部分不联网;执行部分需要本机有 node

    python3 modules/signer/test_signer.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import signer  # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" (期望 {want!r})"))
    if not ok:
        FAILED.append(name)


ENV_PROBE_JS = """
var out = [];
out.push(navigator.userAgent.length > 20 ? 'ua-ok' : 'ua-bad');
out.push(navigator.platform);
out.push(screen.width + 'x' + screen.height);
out.push(typeof document.cookie);
out.push(localStorage.getItem('none') === null ? 'store-ok' : 'store-bad');
out.push(window.location.href);
out.push(btoa('hi'));
out.push(typeof URL === 'function' ? 'url-ok' : 'url-bad');
out.push(typeof URLSearchParams === 'function' ? 'usp-ok' : 'usp-bad');
out.push(typeof eval === 'function' ? 'eval-ok' : 'eval-bad');
out.push(typeof crypto.getRandomValues === 'function' ? 'crypto-ok' : 'crypto-bad');

// 展开这些容易崩的用法:符号属性、迭代、then、深层未知属性
out.push([...window.someUnknownArray].length === 0 ? 'iter-ok' : 'iter-bad');
out.push(typeof window.deep.nested.value);
out.push(typeof neverDefinedAnywhere);
for (var k in window.someUnknownObject) { out.push('loop'); }
out.push([1,2,3].map(function(x){ return x * 2; }).join(','));
out.push(JSON.stringify({a: 1}));

function sign(text) { return out.join('|') + ' >> ' + text; }
"""


def test_static_analysis():
    print("\n[静态分析] 不需要 node")
    code = ("function sign(a, b) { return a + b; }\n"
            "var helper = function() {};\n"
            "var arrow = (x) => x;\n"
            "var md5impl = '0x67452301';\n"
            "CryptoJS.AES.encrypt(x);\n"
            "var SALT = 'n%A-rKaT5fb[Gy?;N5@Tj';\n")
    funcs = signer.list_functions(code)
    check("识别 function 声明", "sign" in funcs, True)
    check("识别 var 赋值函数", "helper" in funcs, True)
    check("识别箭头函数", "arrow" in funcs, True)

    crypto = signer.detect_crypto(code)
    check("识别 MD5 常量", "MD5" in crypto, True)
    check("识别 AES 调用", "AES" in crypto, True)
    check("识别 CryptoJS", "CryptoJS" in crypto, True)

    salts = [s for s, _ in signer.find_salts(code)]
    check("挑出可疑盐值", "n%A-rKaT5fb[Gy?;N5@Tj" in salts, True)
    check("不把 MIME 当盐值",
          any("application/json" in s for s in salts), False)

    params = signer.find_sign_params('x["x-zse-96"]=1; y["sign"]=2; z["nonce"]=3;')
    check("识别签名参数名", {"x-zse-96", "sign", "nonce"}.issubset(set(params)), True)

    env = signer.find_env_touches("navigator.userAgent; document.cookie; window.x;")
    check("统计环境依赖", {"navigator", "document", "window"}.issubset(env.keys()), True)


def test_sandbox():
    print("\n[补环境沙箱] 需要 node")
    node = signer.find_node()
    if not node:
        print("  SKIP  本机没有 node")
        return
    tmp = Path(tempfile.mkdtemp()) / "probe.js"
    tmp.write_text(ENV_PROBE_JS, encoding="utf-8")
    res = signer.run_js(tmp, func="sign", args=["ARG"], timeout=20)

    check("执行成功", res.get("ok"), True)
    if not res.get("ok"):
        print(f"       错误: {res.get('error')}")
        return
    parts = dict(zip(
        ["ua", "platform", "screen", "cookie", "store", "href", "btoa", "url",
         "usp", "eval", "crypto", "iter", "deep", "undef"],
        str(res["result"]).split(" >> ")[0].split("|")[:14]))
    check("navigator.userAgent 可信", parts["ua"], "ua-ok")
    check("platform 为真实值", parts["platform"], "Win32")
    check("screen 为真实值", parts["screen"], "1920x1080")
    check("document.cookie 类型正确", parts["cookie"], "string")
    check("localStorage 可用", parts["store"], "store-ok")
    check("location 为真实 URL", parts["href"], "https://example.com/")
    check("btoa 可用", parts["btoa"], "aGk=")
    check("URL 已从 node 继承", parts["url"], "url-ok")
    check("URLSearchParams 已继承", parts["usp"], "usp-ok")
    check("eval 已继承", parts["eval"], "eval-ok")
    check("crypto 已模拟", parts["crypto"], "crypto-ok")
    check("展开未知数组不崩", parts["iter"], "iter-ok")
    check("深层未知属性有替身", parts["deep"], "function")
    check("完全未定义的全局有替身", parts["undef"], "function")
    check("参数传入正确", str(res["result"]).endswith(">> ARG"), True)

    missing = res.get("missing") or []
    check("记录了未定义访问", any("neverDefinedAnywhere" in m for m in missing), True)

    # 不存在的函数要给出可用列表,而不是静默失败
    bad = signer.run_js(tmp, func="noSuchFunction", timeout=20)
    check("调用不存在函数时失败", bad.get("ok"), False)
    check("失败时给出可调用列表", "sign" in (bad.get("callables") or []), True)


def test_error_location():
    print("\n[错误定位]")
    if not signer.find_node():
        print("  SKIP  本机没有 node")
        return
    tmp = Path(tempfile.mkdtemp()) / "broken.js"
    # 用真实值触发错误 —— 未定义对象会被沙箱兜住,测不出报错定位
    tmp.write_text("var a = 1;\nvar b = 2;\nvar c = null;\nc.property.access;\n",
                   encoding="utf-8")
    res = signer.run_js(tmp, raw="1", timeout=20)
    check("加载报错被捕获", res.get("ok"), False)
    ctx = res.get("context") or ""
    check("指出出错行号", ":4" in ctx or " 4 |" in ctx, True)
    check("附带源码上下文", "property.access" in ctx, True)


if __name__ == "__main__":
    print(f"signer 自测 (node: {signer.find_node() or '未安装'})")
    test_static_analysis()
    test_sandbox()
    test_error_location()
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {', '.join(FAILED)}")
        sys.exit(1)
    print("全部通过")
