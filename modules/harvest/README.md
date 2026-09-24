# harvest —— 爬取执行器

scout 负责判，harvest 负责收。

`scout` 告诉你这个站该怎么采，`harvest` 按那个结论去采——**后端选择由侦察结果驱动，不做无脑轮询**。

## 用法

```bash
python3 modules/harvest/harvest.py https://books.toscrape.com/
python3 modules/harvest/harvest.py example.com --json
python3 modules/harvest/harvest.py example.com --css ".product_pod h3 a" --max-chars 500
python3 modules/harvest/harvest.py urls.txt -o out/ --concurrency 32
python3 modules/harvest/harvest.py example.com --crawl 200 -o out/
python3 modules/harvest/harvest.py --check          # 看后端与当前预设
```

## 结果与预算

- `--crawl N` 最多抓取 N 个 URL；重复正文、失败响应同样占预算，不为凑齐 N 份不同正文无限爬。重复页仍可贡献待爬链接。每 URL 的网络重试由 `--retries` 控制。
- `--max-crawl-attempts M` 只能进一步降低抓取预算；不是额外配额。
- `--out` 的 JSON/Markdown 页文件不覆盖已有内容，大小写/Unicode 归一化碰撞会分配新名字。索引中的 URL 对应实际文件。未知或不一致的已有 `index.json` 会明确拒绝覆盖；换空目录后重试。
- 默认严格验证 HTTPS。`--ca-file FILE.pem` 在 HTTP 后端追加信任根，不替换系统根；浏览器后端不支持该参数时明确失败，不静默忽略。`--insecure` 是需主动选择的禁用验证选项，不作为默认排障建议。
- 失败、人工超时、匿名降级分别保留状态；降级结果不等于获得登录后完整内容。

## 限制与开关

**所有限制默认放开，但一个都没删。** 需要收回时把对应开关打开或调低即可。

| 项目 | 默认 | 开关 |
| --- | --- | --- |
| robots.txt | **不检查** | `--respect-robots` |
| 速率 | **不限速** | `--rate N`（每域名每秒请求数，0 为不限） |
| 并发 | 32 | `--concurrency N` |
| User-Agent | **每个请求轮换** | `--no-rotate-ua` |
| 连接复用 | 开 | `--no-keepalive` |
| 响应体上限 | 10 MB | `--max-body BYTES` |
| 重定向 | 10 跳 | 预设内的 `max_redirects` |
| 超时 | 15s | `--timeout N` |
| 重试 | 2 次（仅对 408/429/5xx 退避重试） | `--retries N` |
| 代理 | 无 | `--proxy URL` / `--proxy-file FILE`（按请求轮换） |
| Cookie | 无 | `--cookie "a=1; b=2"` / `--cookie-file FILE`（JSON 或 Netscape） |
| 失败模式的自动升级 | 开 | 由 scout 判定驱动 |

三档预设，一键切换：

```bash
--profile aggressive     # 默认:不限速不限并发不查 robots,UA 轮换
--profile balanced       # 2 rps / 8 并发 / 查 robots / 不轮换 UA
--profile conservative   # 0.5 rps / 2 并发 / 查 robots / 不用连接复用
```

单个参数总是覆盖预设值：`--profile conservative --rate 0` 就是「查 robots，但别限速」。

## 请求路径与后端

请求路径按成本递进，`auto` 模式只在必要时升级：

| 路径 / 后端 | 做法 | 依赖 |
| --- | --- | --- |
| `static` | **curl_cffi** 会话：TLS/JA3 指纹与真实浏览器一致，走 HTTP/2 | curl_cffi（未装则回落自建连接池 + urllib） |
| 内嵌 JSON 提取（不是后端参数） | 从响应中提取结构化数据，不额外请求 | 标准库 |
| `render` | **patchright**（Playwright 的未检测版本） | patchright 或 playwright |
| `stealth` | 同上 + 反检测注入（patchright 自带反检测，不重复注入） | 同上 |
| `camoufox` | **指纹级反检测**（Firefox 内核）：WebGL / Canvas / 字体 / 插件 / 屏幕全伪装 | camoufox |
| `scrapling` | Scrapling 的 StealthyFetcher | scrapling（未在本机验证） |
| `captcha` | ddddocr 识别图形验证码 | ddddocr |

`auto` 模式在判定「反检测渲染」时**优先 camoufox**（指纹级），只需渲染时用 patchright（启动更快）。

### camoufox 实测指纹

| 属性 | Chromium 无头（patchright） | camoufox |
| --- | --- | --- |
| `navigator.plugins` | 0 | **5** |
| `navigator.webdriver` | 抹掉 | 抹掉 |
| WebGL renderer | 通用虚拟值 | **`ANGLE (AMD, Radeon HD 3200 Graphics Direct3D11 vs_5_0 ps_5_0)`** |
| WebGL vendor | 通用 | **`Google Inc. (AMD)`** |

patchright 处理的是 CDP 痕迹，Canvas/WebGL 那一层它是真的；camoufox 补的正是这一层。还带鼠标轨迹拟人（`humanize`）、出口 IP 与语言时区自动对齐（`geoip`）、设备指纹持久化（`persistent_context`）。

### 渲染等待预算

`networkidle` 在动态站点上**永远不出现**——长轮询、心跳、新闻瀑布会让网络一直"忙"。所以等待有个预算（`render_wait_ms`，默认 6000ms），到点就用当前内容。

试过用自适应替代固定预算（"内容够就走"/"内容稳定就走"），**实测没有稳定收益**：oschina 首页正文在 1,646 / 2,859 / 5,237 / 12,373 / 37,287 字之间随机跳，那是它自己"先出骨架、再持续追加"的行为。它在 2.8s→3.2s 还有个**假平台期**（HTML 6,436B→6,541B，几乎不变），3.59s 才爆发到 528KB——任何中间态判据都是在抛硬币。自适应版本的总时长反而从 11.1s 涨到 12–13s，所以**回退**了。这段留着是免得以后再走一遍。

能快速安静的站点不受预算影响：`quotes.toscrape.com/js` 实测 0.59s 就到 networkidle，立即返回。

### 后端记忆：记失败，不记成功

`~/.armory/backend_memory.json` 按域名记住各后端的历史表现，用来给升级梯子排序。

**记的是失败而不是成功**，理由是不对称成本：轻档（静态）一跳几百毫秒，重档（浏览器）一跳 5–10 秒，差 20 倍。静态永远跑，给它做记忆省不出东西；真正的浪费在每次去重试那些上次就失败过的重档——每一次重试都是一个完整的浏览器启动。

动机是实测出来的：36kr 会在 camoufox（10.4s）和 stealth（18.5s）之间随机，同一站点相邻两次跑能差 8 秒。

```
- 备注: 静态正文仅 35 字,尝试升级后端
- 备注: 后端历史: 首选 camoufox(0.0 小时前成功)
- 备注: 已从静态升级到 camoufox
```

排的是**顺序而不是删除**：冷却中的后端排到最后但仍然保留——站点会变，判死的后端如果永远不试，就再也发现不了它已经恢复。

TTL 分两档：一般失败 24 小时，403/429/503 这类 1 小时（更可能是 IP 的瞬时状态而非站点结构变化）。`--no-backend-memory` 可完全关闭。

## 人工接管（`--handoff` / `--wait-human`）

自动化不该在验证码上硬耗——那是概率游戏，而且风控会记账。正确做法是**把不可自动化的那一步交回给人**。

```bash
# 按需等待(推荐):站点没墙时和普通抓取一样,撞上墙才停下等你
harvest.py <url> --wait-human          # 默认最多等 300s
harvest.py <url> --wait-human 600      # 自定义上限

# 全程接管:直接用你的浏览器抓
harvest.py <url> --handoff --cdp http://127.0.0.1:9222
harvest.py --browser-start             # 起一个带独立 profile 的可见浏览器
harvest.py --browser-info              # 看有没有可接管的
harvest.py --browser-close             # 清理所有 armory 起的浏览器进程
harvest.py --keep-browser              # 自己启的浏览器不自动关(调试用)
```

### 等待与降级的行为

撞墙后**停下**，不再盲目重试；每 **5 秒**探一次墙还在不在；处理完自动继续。

**等满上限就自动放弃人工方式、降级回全自动**——你不在的时候，脚本该自己往下走，而不是卡死：

```
  ⏸  已暂停: 人机验证
  请在浏览器窗口里完成处理。每 5s 检测一次,最多等 300s。
  超时会自动放弃人工方式、降级回全自动继续 —— 你不在也不耽误
    ⏳ 等待中 5s / 300s
```

超时返回的是「当前拿到什么就用什么」+ `degraded: true` 标记，不是失败。

### 弹窗判定：看内容，不看弹窗

**判据不是「页面上有没有弹窗」，而是「弹窗有没有挡住内容」。** 知乎就是典型：登录弹窗挂在那儿，热榜照样读得到——判成「需要人工」会让全自动流程白等五分钟。

```
知乎页面(1479 字文本) → 需要人工=False     ← 弹窗只是浮层
纯登录页(21 字)        → 需要人工=True(内容仅 21 字,疑似被挡)
```

### 浏览器生命周期：默认用完就关

这条是补的坑。此前 `_owns_browser` 分支只打印一句「保留运行中」，于是**每次调用都留下一个浏览器进程**。实测跑一段时间后攒了 **14 个 chromium 进程**（9 个测试 profile + 5 个 handoff profile），分属 2 个进程组——而每个 chromium 本身还是多进程的，合起来能把 CPU 压住。

现在两类资源都记账：

| 资源 | 谁开的 | 收尾动作 |
| --- | --- | --- |
| tab | 我们（`goto` / `page(new=True)`） | `__exit__` 逐个关闭 |
| 浏览器进程 | 我们（`_owns_browser`） | 默认终止整棵进程组 |
| 用户的浏览器 | 用户 | 只断开调试连接，不动 |

接管模式下浏览器是**用户的**，我们开在里面的每个 tab 都是一个独立渲染进程——不关就是在用户的机器上留垃圾。所以 `_own_pages` 逐个记账，`__exit__` 统一关掉。

自己启动的浏览器按**进程组**整组收（`start_new_session=True` + `os.killpg`）：只 terminate 主进程的话，渲染子进程会变成孤儿继续吃 CPU。

```bash
harvest.py --keep-browser     # 自己启的浏览器不关(调试用)
harvest.py --browser-close    # 清理所有 armory 起的浏览器进程(含历史遗留)
```

`render` / `stealth` / `camoufox` 的 `browser.close()` 也都移进了 `finally`——渲染中途抛异常时，原先写在 `try` 末尾的 close 会被跳过，浏览器就留在后台不退了。

实测：清理前 19 个 chromium 进程，`--browser-close` 后剩 1 个；之后连跑 3 次 render、2 次 camoufox，进程数稳定不增长。

## 登录态：存档与体检

人工接管时把 cookie 存下来（`--handoff` 自动落到 `~/.armory/states/<域名>.json`，0600 权限），之后该域名一律走高速静态路径。

```bash
harvest.py <url> --domain-state               # 复用该域名的存档
harvest.py --state-scan                       # 体检全部存档
harvest.py --state-scan --state-scan-offline  # 只看文件,不发请求
harvest.py <url> --state-check                # 探测登录态;不给 URL 就自动选探针
```

### 主动探测：这份凭证现在还认不认

`--state-check` 走两条路：

1. **身份接口**（表内站点）—— 直接打 `/api/v4/me`、`/user` 这类端点。JSON 接口干净得多：401 就是 401，不会像首页那样对匿名用户也返回 200 加满屏"登录"字样。
2. **差分探测**（表外站点）—— 带登录态与匿名各请求一次，比响应差异。这一路是必要的：大量站点对匿名和登录用户返回同一个 200 页面，只差头像和用户名，单看一次请求分不出登录态有没有生效。

```
www.zhihu.com  有效[identity]  28 项 cookie | 0.0 天前保存 | 存档 400 天后过期
               └ 身份接口返回了身份字段
```

判不出来就明说「无法判定」，不猜。

### 离线判据为什么改用最晚的过期时间

一份存档里总有几个短命辅助 cookie —— 知乎的 `unlock_ticket` 只有 30 分钟、`BEC` 1 小时 —— 而真正的登录凭证 `z_c0` 有 180 天。

按**最早**的算，存档会在存下半小时后被判「已过期」而整份拒用，实测撞上过。现在离线这层只挡「所有 cookie 都过期」的死透存档，精确判定交给主动探测。

会话级 cookie 的 `expires=-1`（会被解析成 1969 年）同样不算数，否则每个含 session cookie 的存档都会被误判。

## 验证码

`captcha.py` 里滑块定位有**四种方法**，加权取优：

| 方法 | 原理 | 实测（20 个真实照片背景样本） |
| --- | --- | --- |
| **`locate_slider_rendered`** | **渲染模板**：先把滑块渲染成「缺口应该长的样子」再匹配 | **20/20，平均误差 0.1px** |
| `locate_slider_contour` | 在背景图里找缺口轮廓（几何事实） | 18/20，平均误差 16px |
| `locate_slider_opencv` | Canny 边缘 + 模板匹配 | 命中率更低 |
| `locate_slider` | ddddocr 的 `slide_match` | 0/15，平均误差 139px |

```bash
python3 modules/harvest/captcha.py --slide piece.png bg.png   # 定位缺口
python3 modules/harvest/captcha.py --caps-report              # 看各类可解性
```

### 渲染模板这条路的由来

**我原来的做法是错的**：拿滑块**原图**去匹配背景上的缺口。但缺口是「滑块原图**经过渲染**」后的样子（变暗/调色 + 描边），**模板与目标根本不一致**——所以三方法加权只有 15/20。

正确做法是把滑块**渲染成缺口应该长的样子**再做归一化互相关：

```python
t = 滑块灰度 * keep + offset          # 缺口的明暗变换
cv2.rectangle(t, ..., 255, stroke_px)  # 缺口自带的那圈描边
r = cv2.matchTemplate(背景, t, cv2.TM_CCOEFF_NORMED)
```

`keep` 各站点不同（实测 0.45/0.6/0.8 都能中），不传就**扫一组候选取最优**，等于在线标定——比用 10 张样本做 `polyfit` 拟合更省事。

实测四种场景：

| 场景 | 命中 | 平均误差 |
| --- | --- | --- |
| 标准（keep 0.45，带描边） | **20/20** | 0.1px |
| 无描边 | 18/20 | 7.3px |
| 不同渲染（keep 0.6） | **20/20** | 0.1px |
| 低对比（keep 0.8） | **20/20** | 0.2px |

**两条踩出来的经验**：

1. **置信度不能跨方法比较**。等权重时，边缘匹配的虚高置信度会压过精确结果——「综合最优」只有 1/15，比单用最准的方法（12/12）还差。
2. **测试样本必须贴近真实**。我最初挖的缺口是「纯色方块+白边」，与滑块图的纹理边缘对不上，于是三种方法全军覆没——**结论一度是「滑块本地不可行」，那是样本错了，不是算法不行**。

其余类型：图形码本地可用（实测 5/5）；reCAPTCHA / Turnstile / hCaptcha / 极验 / 腾讯 / 阿里滑块需打码平台；点选式需人工。



自动化不该在验证码上硬耗——那是概率游戏，而且风控会记账。正确做法是**把不可自动化的那一步交回给人**。

```bash
harvest.py --browser-start          # 起一个带独立 profile 的可见浏览器
harvest.py <url> --handoff          # 卡在验证时暂停，你点完，自动继续
harvest.py <url> --handoff --cdp http://127.0.0.1:9222   # 接管你正在用的浏览器
harvest.py --browser-info           # 看有没有可接管的浏览器
```

**为什么这条路是上限方向的正解**——它一次解决三件事：

| 问题 | 常规做法 | 接管形态 |
| --- | --- | --- |
| 指纹 | 伪装（patchright / camoufox） | 就是你的真实浏览器 |
| 登录态 | 导出导入 Cookie | 天然就在 profile 里 |
| 验证 | 本地模型 / 打码平台 | 你顺手点一下 |

这类流量在风控眼里本来就是你的正常浏览。**代价**：需要你人在场，不能无人值守跑。

**关键性质**：程序断开连接**不会**关闭你的浏览器。脚本退出后浏览器照常在，登录态留在 profile 里供下次使用。

### 真实站点实测（知乎）

一次真实的人机协作：程序打开知乎弹出登录框 → **用户手动登录** → 登录态留在 profile 里 → 后续全自动。

三条路径抓同一个页面（`www.zhihu.com/hot`）：

| 路径 | 状态 | 耗时 | 结果 |
| --- | --- | --- | --- |
| 自动化（curl_cffi → camoufox 自动升级） | **403** | 6.44s | 915B 登录引导 |
| **存档登录态 + 静态路径** | **200** | **0.48s** | 6/6 关键词命中 |
| 接管（真实浏览器） | 200 | ~2s | 254KB 完整页面 |

**接管一次，之后这个域名走高速静态路径**——`--handoff` 会把会话按域名存到 `~/.armory/states/<域名>.json`，后续加 `--domain-state` 直接复用：

```bash
harvest.py https://www.zhihu.com/hot --handoff --cdp http://127.0.0.1:9222   # 人工过一次
harvest.py https://www.zhihu.com/hot --domain-state --json                    # 之后 0.48s 静态抓
```

**代价**：接管那一刻需要你人在场。所以它是显式开关，不默认启用。

## 代理池

单 IP 上堆再多技巧也有天花板。`proxypool.py` 管的不是「轮流取一个」，是三件事：

**一、可用的才发。** 健康检测 + 延迟 EMA 评分 + 连续失败冷却。不检测就发，等于把代理的失败率直接叠进抓取成功率。

**二、按站点记账。** 同一个代理在 A 站被封，不该牵连它在 B 站干活。请求拿到 403/429 记的是**站点维度**的账；只有连接失败才记代理本身的账。

**三、自己别暴露自己。** 每次请求换一个出口 IP，对风控来说就是「同一账号在多个城市之间瞬移」——比固定 IP 更可疑。所以默认**会话亲和**：同一站点保持同一出口。

```bash
python3 modules/harvest/harvest.py --check-proxies --proxy-file proxies.txt   # 只体检
python3 modules/harvest/harvest.py https://target.com --proxy-file proxies.txt
```

实测（本地 1 好 1 坏代理）：健康检测正确识别，8 次请求全部走好代理且保持同一出口，坏代理被标记为「劣质」并停止派发。

### 指纹伪装实测（tls.peet.ws）

| 路径 | JA3 | JA4 | 协议 |
| --- | --- | --- | --- |
| 自建连接池 / urllib | `e8df55bd…` | `t13d1711_…` | HTTP/1.1 |
| curl_cffi `impersonate=chrome` | `c5b1f66e…` | `t13d1516h2_…` | **h2** |

JA3 完全不同，且 curl_cffi 能谈到 HTTP/2 —— 之前只有 HTTP/1.1 这一条就足以被识别。指纹类型可换：`--impersonate chrome120 / safari / firefox / edge`。

渲染引擎实测：patchright 1.20s 抓完 JS 渲染页，`navigator.webdriver` 无痕迹。

验证码：`--solve-captcha <IMAGE_URL>` 下载图片并识别，实测对自造样本识别准确。

```bash
python3 modules/harvest/harvest.py --solve-captcha https://site.com/captcha.jpg
```

`auto` 的流程：发一次静态请求 → 用 `scout.build_evidence` + `scout.decide` 判定 → 只有判定为
「动态渲染 / 反检测渲染 / 验证码闸门」时才升级。**一次请求定策略，而不是把后端轮一遍。**

## 性能上限

用 `bench.py` 打本地回环量出来的（见 `bench.md`，含完整数据与踩坑记录）：

- **零延迟下约 4700-5300 QPS**，瓶颈是本机 CPU 与 GIL；并发 1 就能跑满，加到 128 反而轻微下滑
- **连接复用是最大的单个杠杆**：HTTP 下 1.61×，HTTPS 下 **8.85×**（每次建连要付完整 TLS 握手）
- **有 RTT 时吞吐 = 并发 ÷ RTT**，一路线性直到撞上上面那个天花板
- 真实网络实测：深爬 30 页 **1.697 秒**（串行 BFS，约 56ms/页）

## 能力

- **结构化输出**：meta、正文 Markdown、纯文本、链接、表格、内嵌 JSON、统计
- **CSS 选择器**：自建 mini 选择器，支持 `tag`、`.class`、`#id`、`[attr=val]`、后代、`>` 子代、逗号分组
- **正则抽值**：带捕获组时默认取第 1 组
- **HTML → Markdown**：标题、链接、列表、代码块（带语言）、表格、图片、引用
- **批量与深爬**：URL 文件 + 并发线程池；`--crawl N` 同域 BFS
- **限速**：per-domain 令牌桶，不同域名互不排队
- **落盘**：`-o dir` 输出每页 JSON + Markdown 和一份 `index.json`

零第三方依赖即可运行；装上 playwright 自动启用渲染后端（`--check` 查看）。

## 已验证

| 目标 | 结果 |
| --- | --- |
| `books.toscrape.com` | auto 判定「静态直取」，CSS 抽出 20 本书名 |
| `quotes.toscrape.com/js/` | auto 判定「动态渲染」→ 自动升级 render，抽出 10 条名言 |
| `quotes.toscrape.com/js/`（stealth） | 1.13s 渲染成功 |
| 批量 3 URL | 并发抓取 + JSON/Markdown 落盘 |
| 同域深爬 30 页（串行 BFS） | 1.697s |
| 同域深爬 30 页（分层并发） | **0.730s**，30 页零失败 |
| 历史 10 站采样（非当前验收） | 旧记录 11.5s、10/10；修正正文判据后未重新确认，不保证任意站点或登录后正文 |
| 存档复用 + 在线探测 | 知乎 28 项 cookie，身份接口确认「有效」，静态 0.6s / 216KB |
| `test_harvest.py` | 全部断言通过（不联网） |

## 设计取舍

- **判定链与 scout 共用**。`scout.build_evidence` + `scout.decide` 被直接调用，侦察与执行不会得出两套结论。
- **自建 DOM 与选择器**。不引 lxml/bs4：处理单页而非千万页，装依赖的收益抵不过部署成本。
- **连接池是自建的**。urllib 每次请求都重建 TCP+TLS，HTTPS 上单次握手 100-300ms——这不是并发数不够，是每次都重新握手。
- **两条路径共用 TLS 策略**。连接池路径与 urllib 路径用同一个 SSL 上下文，否则证书异常的目标会一条成功一条失败。
- **不静默降级**。判定需要渲染却没有渲染后端时，结果里带明确备注，而不是假装抓成功了。
- **限速按域名而不是全局**。不同站点互不拖累，同站点不越线。

## 已知边界

- `render` / `stealth` 后端需要 `playwright` + chromium；未安装时 `auto` 会降级到 static 并给出备注。
- `scrapling` 适配器按容错写的，本机未装该库，**未经实测**。
- 深爬已做**分层并发**（同层并行、层间串行，`--concurrency` 控制同层并发度）：30 页 **0.730s**（串行时 1.697s）。同域限速仍在引擎里按域名生效，并行不会对单个站点超频。
- 深爬**没有内容指纹去重** —— 指向同一内容的不同 URL 会被各抓一次。
- 登录态支持三种注入：`--cookie` 手工串、`--state` 指定文件、`--domain-state` 按域名自动复用存档；多域名存档用 `--state-scan` 统一体检。
- 身份探针表只覆盖少数常用站点（知乎 / 微博 / B站 / GitHub / X）。表外站点走差分探测，判不出来的会明说「无法判定」而不是猜。
- 存档是明文 JSON，只有 0600 权限保护，**没有做加密存储**。
- 验证码判定得出来，但没接执行（教程 08 章的 OCR / 滑块方案可以直接搬）。
