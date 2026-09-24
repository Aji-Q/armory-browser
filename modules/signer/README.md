# signer —— JS 签名复现器

签名逆向的最后一环：把从站点扒下来的 JS 跑起来，复现出它生成的签名参数。

**难点从来不是算法，是补环境。** 站点 JS 假设自己跑在浏览器里，会访问 `navigator`、`document`、`window` 的几十个属性，缺一个就 `ReferenceError`。素材里 22 个案例的补环境全是手工写的，而且方式各不相同：

```javascript
window=global; window.navigator={userAgent:'...'};   // 知乎
var navigator={};                                     // 网易
this.navigator={}; this.window=this;                  // 淘宝
```

这个模块不逐个猜 API，而是用 node 的 `vm` 建一个 Proxy 沙箱：**未知属性一律返回可链式访问的替身对象**，并把访问过的名字记下来，执行完打给你看。补环境从「猜」变成「看」。

## 用法

```bash
# 1. 先看这堆混淆代码里有什么
python3 modules/signer/signer.py probe target.js

# 2. 在补环境里跑起来
python3 modules/signer/signer.py run target.js sign
python3 modules/signer/signer.py run target.js sign --args '["keyword", 1]'
python3 modules/signer/signer.py run target.js --raw 'sign("keyword")'

# 3. 直接当命令用(配合 harvest 直连接口)
SIGN=$(python3 modules/signer/signer.py run target.js sign --args '["k"]' 2>/dev/null)
python3 modules/harvest/harvest.py "https://api.example.com/search?q=k&sign=$SIGN" --state state.json
```

`probe` 给出：候选函数名、算法特征（MD5/SHA/AES/RSA/HMAC/Base64/CryptoJS/混淆）、可疑盐值常量、签名参数名、碰到的浏览器对象。

## 补环境怎么做

三层兜底，从下往上：

**第一层：从 node 全局继承内置。** `has: true` 的陷阱会把 context 自带的内置对象一并遮蔽，漏写一个就 `ReferenceError`。所以先把 node 的 `URL`、`URLSearchParams`、`eval`、`Intl`、各种 TypedArray 全搬进来，再用浏览器侧的模拟覆盖上去。

**第二层：浏览器对象给真实值。** `navigator.userAgent`、`screen.width`、`location.href`、`localStorage`、`btoa`、`crypto.getRandomValues` 等给出可信的值——签名里常拿它们参与计算，给错值签名就错。

**第三层：Proxy 兜底 + 记账。** 未知属性不返回 `undefined`（下一层访问就崩），而是返回可调用、可迭代、可 await 的替身，同时记进 missing 列表。

符号属性最容易踩坑：`Symbol.iterator` 返回空对象的话，`for...of` 一碰就崩；`then` 返回 `undefined` 的话，`await` 会直接挂起。这两处都给了可调用的替身。

## 实测

对素材 `Crack-JS-Spider` 里全部 29 个从真实站点扒下来的混淆 JS 做加载测试：

**25 / 29 加载成功（86%）**，包括 3718 行的拼多多 `anti_content.js`、21440 行的 Boss 直聘 `zp_token.js`、2362 行的 B 站 `bilibili.js`。

4 个失败的全是**样本自身残缺**，不是环境问题——工具把原因直接指了出来：

| 样本 | 报错 | 真实原因 |
| --- | --- | --- |
| `x-zse-96.js` | `__g._encrypt is not a function` | 第 172 行 `__g = {}` 是空对象，`_encrypt` 全文件只出现一次调用、从未定义——提取时丢了 |
| `toutiao.js` | `Cannot read properties of undefined` | 第 705 行 `jsvmp.sign.call(n,i)`——jsvmp 虚拟机保护的运行时对象不在提取的代码里 |
| `zp_token.js` | `i.p is not a function` | 控制流平坦化产物，依赖的状态对象未随代码一起提取 |
| `qqsign.js` | `Cannot read properties of undefined` | 同上 |

## 错误定位

混淆代码光有报错信息根本定位不到位置，所以失败时会直接给出出错行号和源码：

```
[!] 失败(load): Cannot read properties of undefined (reading 'call')

出错位置:
      704 | i.body = t.data;
      705 | var o = jsvmp.sign.call(n,i);
      706 | return o
```

调用不存在的函数时，会说明命中的是替身，并列出沙箱里**真实存在**的函数（过滤掉 100 多个内置）：

```
[!] 失败(call): not a defined function: notARealFunc —— 命中的是替身,这个函数在目标 JS 里并不存在
    沙箱里可调用的函数: sign, myL, myA, ...
```

## 在工具链里的位置

```
scout    判定「疑似参数签名」(有接口线索 + 有混淆脚本)
  ↓
signer   在补环境里跑通签名函数,复现出参数
  ↓
harvest  带上签名直连接口(--state / --cookie 维持登录态)
```

scout 能识别出签名层但做不了复现，signer 补的正是这一段。

## 设计取舍

- **不让未知属性变成 `undefined`**。真实浏览器里访问不存在的属性就是 `undefined`，但那会让逆向调试寸步难行。替身 + missing 账本更实用。
- **`getElementById` 返回替身而不是 `null`**。真实环境找不到元素返回 `null`，可代码紧接着 `.call()` 就崩。这里选择让代码跑下去。
- **`require()` 返回替身而不是 `{}`**。返回裸对象的话，`.xxx.yyy` 一碰就 `undefined` 报错——这个 bug 实测踩过。
- **替身带 `__armory_stub__` 标记**。否则无法区分「JS 里真有的函数」和「兜底的替身」，调用检查形同虚设。
- **`setTimeout` 同步执行**。签名逻辑常把回调塞进定时器，异步等不到结果；同步跑效果更好。

## 边界

- **站点 JS 在 node 里拥有完整权限**（可读写文件）。只在分析环境里跑，别拿来源不明的 JS 直接执行。
- **加载成功 ≠ 调用成功**。签名函数往往还需要正确的入参和前置状态（比如先调一个初始化函数）。工具负责把环境铺好，业务参数得自己给。
- **不覆盖 jsvmp / wasm 保护**。那类保护需要把整个 VM 运行时一起提取，超出补环境范畴。
- **指纹级检测补不了**。Canvas / WebGL 渲染结果、字体度量、TLS 指纹这些，静态补环境给不出真实值，得靠真实浏览器（见 `harvest` 的 `--backend stealth`）。
