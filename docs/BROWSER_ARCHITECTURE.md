# Armory Browser：实现边界与架构

## 两个目标分别解决

1. **可靠采集**：保留 Python 采集器，补数据覆盖、深爬漏页、失败状态与验收负向测试；不把吞吐、内容完整性和 HTTP 200 混为一谈。
2. **浏览器协作**：不是把 Python 强塞进扩展，而是增加真实页面 DOM 采集器、任务协议与人机协作界面。Python CLI 和浏览器 DOM 是两个执行入口，浏览器无需安装 Python。

```text
Claude Code / Codex
  │ MCP stdio：capture / status / cancel
  ▼
MCP 适配器（和 Agent 同一运行环境）
  │ HTTPS + agent token
  ▼
单用户 Relay：SQLite 持久化任务、结果和状态
  ▲ 浏览器主动拉取任务；不接受远程 JS
  │ HTTPS + 独立 browser token
Chrome MV3 侧栏 → 首次站点/会话授权 → 自动采集 → 自动回传
                         │
                         └ 真正挡住正文：限时请求人工 → 登录成功恢复 / 超时回退匿名全自动
```

**云端条件**：若 Agent 与浏览器不在同一机器，Relay 必须部署到双方可访问的 HTTPS 服务。`127.0.0.1` 仅能本机联调，不是云端联通。此版本没有部署、没有 OAuth 多租户、没有接入任何用户的模型账户。

## 协作状态

`queued → running → awaiting_share → completed`

登录墙分支：`running → awaiting_human → running`。人工成功则恢复原任务；超过服务器记录的 human_deadline_at（默认300秒）由插件发 timeout 转回 running，尝试不带 Cookie 的公开响应，部分结果标记 degraded=true / quality=partial。没有真实可用正文则明确失败，不伪造成功，也不阻塞其他任务。

任何未完成状态可以取消或失败。Agent 只能提交/查看/取消；浏览器执行需要用户先按站点授权当前会话的自动采集和回传范围（最长8小时，可撤销）。范围内任务自动执行和回传，不逐任务打扰。awaiting_share 是提交中间态，也用于授权撤销后保留本地预览。没有相应授权时正文不进入中转。

页面身份和任务身份分开：job id 在 Relay 持久化，tab id 仅在浏览器；仅原任务 origin 可提取。跳到身份提供商时，不采集该登录页；用户完成后回到原网站再恢复。

## 明确的能力边界

- 只采集用户可见且有权访问的页面，不承诺所有网站成功，不破解权限/付费墙或自动解验证码。
- 不索取 cookies/debugger/webRequest 权限；不上传 cookie、密码、表单值、storage、原始 HTML。
- 正文仍可能含个人信息：首次授权会明确自动回传范围和 Relay 地址，支持撤销；仅本地模式可先预览再导出。“不读取密码”不等于“正文没有敏感数据”。
- 页面返回内容是**不可信资料**，不能让页面中的提示词变成 Agent 指令。
- 扩展 JavaScript 全部随包；云端仅能发受限 JSON 任务。没有远程脚本/eval。
- 首版侧栏打开时约 2 秒轮询；关闭、断网或 Chrome 退出时任务等待。不是全天候后台实时控制器。
- 支持 DOM 正文/Markdown/链接等基本抽取；跨域 iframe、PDF、复杂虚拟列表、全站自动翻页、浏览器重启后的无感任务恢复仍需专项实现和验收。
- relay 是单用户参考实现，公网生产还需要 TLS 终止、限流、密钥轮换、监控、部署备份与删除策略；不能当已审计的生产服务。

## Chrome 分发

MV3 和侧栏是合适的技术形式，但能加载不等于可上架。商店发布还需实际浏览器测试、开发者账户、图标/截图、公开隐私政策、准确权限及数据用途披露，并由 Google 审核。此轮不代用户提交或付费。

## 依据（本轮核验官方文档）

- [Chrome Side Panel API](https://developer.chrome.com/docs/extensions/reference/api/sidePanel)：MV3 侧栏 API 与用户交互约束。
- [Chrome 权限声明](https://developer.chrome.com/docs/extensions/develop/concepts/declare-permissions)：按功能选择权限，optional_host_permissions 在使用时授权。
- [MV3 附加要求](https://developer.chrome.com/docs/webstore/program-policies/mv3-requirements)：扩展逻辑应随包，不能用远程代码补功能。
- [Chrome 用户数据政策](https://developer.chrome.com/docs/webstore/program-policies/user-data-faq)：需要明确披露采集、用途与传输。
- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)：UTF-8 换行分隔 JSON-RPC；stdout 只输出协议消息。
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)、[Codex MCP](https://developers.openai.com/codex/mcp)：可配置本地 stdio 工具服务。客户端实际接通仍需在用户配置中注册，本轮未擅自修改其配置。
