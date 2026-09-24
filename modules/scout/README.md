# scout —— 站点采集侦察器

拿到一个 URL，用最少的普通 HTTP 请求判明：**这个站该怎么采、会撞上什么**。

这是 armory 的第一个产出模块。它不抓数据，只做决策——因为素材里十几种抓取实现都卡在同一处：上手前不知道该派哪种抓法。

## 用法

```bash
python3 modules/scout/scout.py https://example.com
python3 modules/scout/scout.py example.com --json        # 机器可读
python3 modules/scout/scout.py example.com --no-naive    # 跳过裸 UA 对照请求
python3 modules/scout/test_scout.py                      # 自测,不联网
```

零依赖，纯标准库。默认发两个 GET：基线请求（完整浏览器头）+ 裸 UA 对照请求。

## 判定维度

六个层级取自 `inbox/CrawlerTutorial` 的对抗梯度（02 章《反爬虫对抗基础》）：

| 层级 | 判定依据 | 教程对应 |
| --- | --- | --- |
| 请求特征层 | 裸 UA 与浏览器 UA 的响应差异 | 02 章 UA / 请求头检测 |
| 行为特征层 | 限流响应头、`Retry-After` | 02 章 频率检测 |
| 会话层 | 401 / 重定向到登录页 | 06、07 章 登录与 Session |
| 签名层 | 接口线索 + 混淆脚本并存 | 02 章 API 签名检测 |
| 指纹层 | JS 挑战、指纹采集脚本、CDN 挑战凭证 | 05 章 浏览器指纹检测 |
| 验证码层 | 验证码厂商特征串 | 08 章 验证码识别 |

## 策略映射

| 判定 | 含义 | 出口 |
| --- | --- | --- |
| 静态直取 | 首屏 HTML 即数据 | httpx/requests + 完整浏览器头 |
| 内嵌数据直取 | 首屏含可解析 JSON | 直接解 `__NEXT_DATA__` / `__NUXT__` 等，免渲染 |
| 动态渲染 | 数据由 JS 注入或 XHR 取回 | Playwright 无头 / crawl4ai 的 AsyncWebCrawler |
| 反检测渲染 | 存在 JS 挑战或指纹采集 | Scrapling 的 StealthyFetcher + stealth.js |
| 验证码闸门 | 命中验证码 | OCR / 打码平台，并务必配代理池 |

验证码与 JS 挑战是硬门槛，优先级高于内嵌数据——能省渲染不等于能过闸门。

## 已验证样本

公开沙箱站点（爬虫练习站）：

| 目标 | 判定 | 依据 |
| --- | --- | --- |
| `example.com` | 静态直取 | 文本 25.4% / 脚本 28% |
| `books.toscrape.com` | 静态直取 | 文本 3.6% / 脚本 **2%**（标签膨胀，非 JS） |
| `quotes.toscrape.com/js/` | 动态渲染 | 文本 1.7% / 脚本 **78%** |
| `scrapethissite.com/pages/ajax-javascript/` | 动态渲染 | 空 `<tbody id="table-body">` + `$.ajax(` |

真实站点：

| 目标 | 判定 | 抓到的证据 |
| --- | --- | --- |
| 豆瓣 | 验证码闸门 | 裸 UA 被拒 **418**，浏览器 UA 200；图形验证码 |
| 微博 | 反检测渲染 | 脚本占 **98%**；重定向到 `passport.weibo.com/visitor/`；`navigator.webdriver` 采集 |
| 知乎 | 验证码闸门 | 裸 UA 被拒 **403**；跳 `/signin`；React 挂载点 + 验证码 |

三个真实站点得出三种不同结论，这正是这个工具存在的意义。

## 已知边界

- **行为特征层只读响应头**。真实限流阈值需要多请求采样才能测出——工具刻意不测，那会把 IP 送进去。
- **指纹层只能看服务端下发的挑战痕迹**。Canvas / WebGL 级别的检测要真实浏览器环境才能验证，静态请求看不到。
- **签名层是启发式推断**。「有接口线索 + 有混淆脚本」只是提示参数可能带签名，不构成确证。
- **陷阱检测基于静态特征**，会有漏报；高置信那档只留诱饵表单字段与负偏移定位，`display:none` 链接单独列出仅供参考（响应式设计里是常规做法）。
- **裸 UA 对照会多打一次请求**。目标敏感时用 `--no-naive`。

## 设计取舍

- **文本比单独用会误判**。`books.toscrape.com` 的 51KB 页面只有 3.6% 可见文本，但脚本占比 0%——低文本比来自标签膨胀，不是 JS 注入。所以判定看的是**文本比与脚本占比的组合**。
- **挂在 CDN 后面不等于会被指纹检测**。`example.com` 在 Cloudflare 后面，但它只是基础设施。指纹层判高要求出现挑战凭证（`cf_clearance` 等）或指纹脚本。
- **页面有内容也可能数据在 XHR 里**。`scrapethissite` 的 AJAX 页文本比 5.7%、脚本 46%，两个页面级信号都不过线；只有「空数据容器 + 异步调用」的组合能抓住它。
