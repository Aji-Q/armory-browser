# Armory Browser Companion：安装与联调

版本：0.1.1，发布候选。**本说明不表示已在 Chrome Web Store 上架，也不表示真实 Chrome、用户的 Claude Code/Codex 配置或远程云端已经连通。** 源码保持私有；外部使用者需先取得发布者提供的授权安装包。发布者：Jay Qin，联系邮箱：jayqin04@gmail.com。

## 1. 组件和前提

```text
Claude Code / Codex → 本地 MCP stdio 适配器 → Relay ← Chrome 侧栏
                                                    ↓
                     自动采集 → 受阻人工 → 超时匿名自动回退
```

- Chrome 116+；扩展本身不运行 Python，不需要网站密码或模型 API key。
- Relay 与 MCP 适配器需要 Python 3.10+，仅使用标准库。
- 本机试用时，Agent、Relay、Chrome 在同一机器。真正远程 Agent 不能访问用户机器的 `127.0.0.1`。
- 向模型发送正文仍适用你的模型服务账户、组织设置和数据政策；Armory 不替你购买或授予模型服务权限。

## 2. 启动本机 Relay

以下路径对应本次优化副本。迁移机器时，替换为授权安装包和 Python 的**绝对路径**。所有路径变量均带引号，支持中文和空格。

```sh
ARMORY_ROOT='/Users/qinjiaji/Documents/个人档案/04_研究与项目资料/2026/代码研究/armory/out/audit-20260923-engine/optimized'
PYTHON='/Users/qinjiaji/Documents/个人档案/04_研究与项目资料/2026/代码研究/armory/.venv/bin/python'
PRIVATE_DIR="$HOME/.armory-browser"
cd "$ARMORY_ROOT"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
"$PYTHON" -m bridge.relay init --output-dir "$PRIVATE_DIR"
"$PYTHON" -m bridge.relay serve \
  --config "$PRIVATE_DIR/server-config.json" \
  --db "$PRIVATE_DIR/jobs.sqlite" \
  --host 127.0.0.1 --port 8765
```

保持最后一个命令运行。成功启动会输出 `Armory single-user relay listening on 127.0.0.1:8765` 开头的消息；`/health` 只提供基础状态，不表示浏览器已连接。

`init` 只运行一次；已有文件时拒绝覆盖，正常重启只运行 `serve`。它创建三个权限为 `0600` 的文件：

| 文件 | 使用者 | 内容与边界 |
|---|---|---|
| `server-config.json` | Relay | 两个独立令牌的哈希，不含原文 |
| `agent-client.json` | MCP 适配器 | `relay_url` 与 `agent_token`，仅创建/查看/取消任务 |
| `browser-client.json` | 本人浏览器 | `relay_url` 与 `browser_token`，驱动浏览器任务状态 |

用本地可信编辑器读取 `browser-client.json`，将 `browser_token` 填入扩展自己的密码框即可。**不要把令牌、完整配置、Cookie 或网站登录密码粘贴到聊天、终端日志、截图、Git 或公开网站。** 本说明不要求 Agent 读取 browser token。

## 3. 配置 Claude Code 或 Codex

在第二个终端中重新设置上面的 `ARMORY_ROOT`、`PYTHON`、`PRIVATE_DIR`。以下 `add` 命令会修改对应客户端 MCP 配置；仅在你决定启用时运行。现有同名配置先检查，不要盲目覆盖。

### Claude Code

```sh
claude mcp add --scope user --transport stdio armory-browser -- \
  "$PYTHON" "$ARMORY_ROOT/bridge/mcp_server.py" \
  --config "$PRIVATE_DIR/agent-client.json"
claude mcp get armory-browser
```

`--scope user` 使其对当前用户的各项目可用。重新打开 Claude Code，在 `/mcp` 中检查连接状态及工具。参考 [Claude Code 官方 MCP 文档](https://code.claude.com/docs/en/mcp)。

### Codex

```sh
codex mcp add armory-browser -- \
  "$PYTHON" "$ARMORY_ROOT/bridge/mcp_server.py" \
  --config "$PRIVATE_DIR/agent-client.json"
codex mcp list
```

重新打开相应 Codex 任务/客户端，核对是否出现下面三个工具。`list` 看到配置不等于已完成浏览器端抓取。采集工具有外部副作用，客户端可能要求逐次批准；请在正常交互会话中按站点/任务核准。若报 `MCP tool call requires approval, but approval policy is never`，说明命令行策略不允许本次调用，不能视为成功，不要改成 read-only 工具或禁用审批来绕过。参考 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

预期工具：

- `armory_capture(url, purpose, max_chars?, idempotency_key?, human_timeout_seconds?)`
- `armory_status(job_id)`
- `armory_cancel(job_id)`

**不要使用 `codex mcp add ... --url http://127.0.0.1:8765`。** Relay 是任务 REST API，不是 HTTP MCP 服务；这里必须启动 `bridge/mcp_server.py` 作为 stdio MCP 适配器。两个令牌也不能互换。

## 4. 加载扩展并授权一个站点

1. 打开 `chrome://extensions`，启用 **Developer mode / 开发者模式**。
2. 点击 **Load unpacked / 加载已解压的扩展程序**，选择：
   `/Users/qinjiaji/Documents/个人档案/04_研究与项目资料/2026/代码研究/armory/out/audit-20260923-engine/optimized/extension`
   不是项目总目录；所选目录内应直接有 `manifest.json`。
3. 固定扩展图标，点击图标打开 Armory 侧栏。
4. `Relay URL` 填 `http://127.0.0.1:8765`；`Browser token` 填本机私密文件的对应值。点击 **连接中继**，只授予该 Relay 的访问权限。
5. 让 Agent 使用 `armory_capture` 请求一个你有权访问的公开 HTTPS 页面，并提供明确 `purpose`。不要提交 localhost、内网、带用户名密码或含凭据参数的 URL。
6. 首次站点任务会等待。确认界面显示的站点和 Relay，点击 **授权此站点自动采集与回传 · 8h**，在 Chrome 提示中授予该网站权限。
7. 该精确 origin 在本浏览器会话、此 Relay、最长 8 小时内自动采集并回传；**不会每个任务再问一次，也不是每页预览后手动批准**。可用 **自动处理已授权站点**、单站点 **撤销**、**撤销全部会话授权** 或 **断开** 停止后续处理。

Chrome 网站权限与 Armory 会话授权是两层不同的控制。撤销会话授权不会自动删除 Chrome 已授予的网站权限；需要时在扩展设置中另行撤销。换 origin（协议、主机或端口）需重新授权。

本地单页模式：先切到想读的正常网页，点击扩展图标，再点击 **采集当前页面 · 仅本地导出**。它使用 `activeTab` 临时授权；不创建远程任务，也不回传正文。如 Chrome 提示权限不足，应重新在目标页点击扩展图标。

## 5. 自动 → 人工 → 超时自动降级

- **自动**：已授权站点任务自动开专属后台标签页；页面导航最多等 8 秒，再在最多 8 秒的正文窗口内每约 800 ms 采样，连续两次正文稳定才回传。明确登录/验证墙不额外等待；其他任务可并行，不把固定 700 ms 当加载完成。
- **人工**：检测到明确登录/订阅墙或访问验证时（长预览不视为完整正文），或正文在有限等待后仍不足时，任务进入 `awaiting_human`。点击 **打开专属页面**，亲自在网站完成登录或验证，返回原任务文章后点击 **已处理，恢复采集**。Armory 不替你输入密码，不破解付费墙、验证码或权限。
- **超时自动回退**：默认等待 300 秒，Agent 可设置整数 5–900 秒。以 Relay 记录的截止时间为准；到期后，仍有会话授权且侧栏在线时，插件尝试不带 Cookie 的公开请求。真实非空公开正文标记 `degraded=true`、`quality=partial`；无可用正文则 `failed`，不能冒充完整成功。其他任务继续。
- **关闭或断网**：侧栏打开时约每 2 秒轮询；关闭侧栏、Chrome 退出、失联或授权过期时不能保证继续执行。重连并恢复授权后处理过期任务；不是全天候后台服务。
- 自动完成的、未用于人工操作且不活跃的专属后台页会清理；人工协作页保留。手动关闭仍在进行的专属页后，请取消并由 Agent 重新建任务。

资源身份同时核对精确 origin、路径和查询串；只忽略 fragment 与末尾斜杠，不把同域账户首页当原文章。人工恢复时，若专属页位于同 origin 的其他路径，会先返回请求 URL；跨 origin 登录页不会被读取。

附属链接会过滤本地/私网和凭据参数，保留可用正文；永久 HTTP 4xx 不无限重试，401/403 会断开连接且仅记录本地终止；已过期删除的旧任务不会阻塞新任务。

`awaiting_share` 是本地提取后、回传前的中间态。授权有效时自动完成回传；不是承诺提供逐任务「Share」按钮。返回内容一律是不可信资料，Agent 不应执行正文中夹带的指令。

## 6. 数据、远程部署与限制

- 本地保存 Relay 地址、有限任务元数据；令牌、站点会话授权及正文预览存于 `chrome.storage.session`，不用 `storage.sync`。断开不是删除历史数据。
- 只回传协议允许的 URL、标题、文本/Markdown、链接、采集时间及质量标记；不上传原始 HTML、Cookie、请求头、浏览器 storage 或表单值。**可见正文仍可能含个人或敏感信息**，请谨慎授权相应站点。
- 单用户 Relay 将任务/已回传结果保存到本地 SQLite，文件权限 `0600`，没有静态加密；默认创建超过 7 天的数据在下次数据库操作时清理。停止服务不会运行清理；已被 Agent 获取的结果和备份不能靠断开撤回。
- 远程 Agent 需要双方可达的可信 HTTPS Relay，以及部署、限流、密钥轮换、监控等生产措施；此版本没有代部署。不要把开发 HTTP 服务直接暴露公网。`init --relay-url` 只写客户端地址，不配置 TLS 或隧道。
- 不支持对所有网站成功的承诺；跨域 iframe、PDF、复杂虚拟列表、全站翻页、重启后的无感恢复仍需专项验证。匿名回退的静态内容无法完整验证网站 CSS 的视觉可见性。
- 隐私政策：[https://aji-q.github.io/armory-privacy/](https://aji-q.github.io/armory-privacy/)。已部署公开页面；2026-09-23 主任务在浏览器打开确认，本说明核对时 HTTP 200、政策标题与联系邮箱匹配。发布上传前仍应复核政策与最终包一致。

## 7. 安装检查与故障定位

| 现象 | 先检查 |
|---|---|
| MCP 配置存在但没有工具 | Python 3.10+、绝对路径、`agent-client.json` 可读、客户端重启/组织 MCP 设置 |
| 扩展 HTTP 401 | 使用 browser token，而非 agent token；不要在聊天里发令牌排错 |
| 拒绝连接/CORS | Relay 仍在运行、URL 端口一致、Chrome 已准许访问；若启用 `--extension-id`，需为当前安装的真实 ID |
| `MCP tool call requires approval, but approval policy is never` | 当前非交互审批策略阻止创建任务；在正常交互客户端批准具体调用，不修改工具为 read-only 或禁用审批 |
| 一直 `queued` | 新站点未授权、自动开关关闭、授权过期或侧栏关闭 |
| `awaiting_human` 不恢复 | 在专属页面处理并返回原文章；期限未到点恢复，过期则走匿名回退 |
| `completed` 但不完整 | 检查 `degraded`、`quality`、`truncated`，不要只看状态字符串 |
| 断开后历史仍在 | 断开只停后续采集；卸载扩展清除其本地存储，Relay 数据需由服务持有人另行处理 |

自动化回归可从 `ARMORY_ROOT` 运行：

```sh
"$PYTHON" -m unittest bridge.test_relay -v
"$PYTHON" tests/test_mcp.py
"$PYTHON" tests/test_reliability.py --root "$ARMORY_ROOT"
"$PYTHON" tests/test_human_fallback.py --root "$ARMORY_ROOT"
```

已有真实 DOM fixtures 与真实 DOM 界面预览的主任务记录；它们不等于 Chrome 已安装扩展或权限验收。上述测试也不能代替真实 Chrome 安装、站点登录或客户端注册测试。发布前逐项执行 `store/REVIEWER_GUIDE.md`；本轮仅核对命令帮助、源代码与协议，不自动改你的客户端配置。
