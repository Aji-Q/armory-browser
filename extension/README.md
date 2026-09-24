# Armory Web Capture 0.1.2

一个无框架、无构建步骤的 Chrome MV3 网页正文采集工具。**不是演示页面。**

## 最短使用路径

1. 在 Chrome 扩展管理页加载本目录（目录内有 `manifest.json`）。
2. 打开要采集的网页，点击 Armory 扩展图标。
3. 点击 **抓取当前页面**，核对来源、正文、字符数和截断标记。
4. 点击 **导出 Markdown** 或 **导出 JSON**，得到真实文件。

这一条路径不需要 Python、Relay、Agent、模型 API key 或网站密码。
本地结果留在当前浏览器会话，不上传。失败时会明确提示，已有结果标明仍是上次采集。
当前单页提取上限为 20,000 字符，超出显示「已截断」。它不会递归遍历链接。
遇到登录墙，请本人先在网站登录，再点击抓取；本地按钮不启动自动登录/回退任务。
权限不足时，请重新在目标网页点击扩展图标，取得 Chrome 的 `activeTab` 临时授权。

## Agent 任务是可选项

展开 **连接 Agent（可选）**，配置你信任的 Relay 和 Browser token。
完整配置见 [安装说明](../docs/INSTALL_BROWSER.md)。Claude Code / Codex 通过 stdio MCP
派发 URL；插件逐站点授权后按同一条状态机运行：

```
自动提取 → 正文可用 → 自动回传
         → 受阻/有限等待后不足 → 请求人工处理
                            → 用户恢复 → 重新提取
                            → 超时 → 匿名请求 → 部分正文或明确失败
```

- 只有明确授权的 origin（协议、主机、端口）能自动采集/回传。授权绑定 Relay，最长 8 小时，可撤销。
- 默认人工期限 5 分钟，以服务端时间为准；其他任务不被阻塞。不会填密码或破解访问限制。
- 匿名回退不带 Cookie、不跟随重定向，标记 `degraded=true / quality=partial`；墙文本不能当成功。
- 侧栏打开时约每 2 秒轮询，最多 3 个任务并行；**关闭侧栏暂停处理**。
- 本地抓取与 Agent 回传分开；连接 Agent 不会自动上传本地结果。

## 代码怎么读

```
sidepanel.js → controller.mjs → extract.mjs       本地提取
                           → bridge.mjs          Agent HTTP 传输
                           → fallback.mjs        超时匿名提取
              core.mjs                           纯状态与结果校验
background.js                                    打开侧栏、任务锁
```

保留小而清楚的模块边界，不新增框架、插件注册器、通用工作流引擎或运行时依赖。
参考的是 [Karpathy nanoGPT](https://github.com/karpathy/nanoGPT) 的可读核心代码思路，
不是强行单文件化。`demo/` 仅是开发夹具，不在扩展包里、不参与实际采集，也不是产品入口。

## 边界

Chrome 116+。正文抽取是启发式，不含 OCR、PDF 解析、跨域 iframe 或 shadow DOM 遍历。
不读取 Cookie、密码、表单值、浏览器 storage 或原始 HTML；不绕过登录/付费/访问限制。
没有默认全站权限、远程代码、CDN、npm、Cookie 导出或调试权限。

令牌、授权、正文预览只存 `storage.session`；Relay 地址与有限任务元数据存 `storage.local`。
可见正文仍可能含敏感信息，请谨慎选择授权站点和中继。断开无法撤回已发送的结果。
此版本未宣称已通过真实 Chrome 安装、真实账户登录、云端 Agent 或商店上架验收。

## 开发检查

在候选项目根目录执行：

```sh
node --test tests/test_extension.mjs tests/test_controller.mjs tests/test_extraction_quality.mjs tests/test_capture_contract.mjs tests/test_minimal_panel.mjs
```

这些是函数/模拟 DOM/Chrome 接口测试，不冒充真实浏览器测试。
