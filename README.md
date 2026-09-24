# Armory Web Capture 0.1.2

私有源码仓库。简洁的 Chrome 网页采集工具，附带可独立运行的命令行爬虫。
**自动采集 → 必要时本人登录 → 等待超时后匿名自动回退。**

## 直接使用

- **Chrome**：加载 `extension/`，打开网页 → 点击扩展图标 →「抓取当前页面」→ 导出 Markdown / JSON。不需要 Relay、Agent 或模型 API key。见 [扩展说明](extension/README.md)。
- **命令行**：Python 3.11+（本机验证为 3.12），在仓库根目录执行下面的命令。静态后端可仅用标准库。

```sh
python3 modules/harvest/harvest.py https://books.toscrape.com/ \
  --backend static --no-impersonate --profile balanced --json --out out/capture
```

`out/capture/index.json` 索引实际生成的 JSON/Markdown。`--crawl N` 最多抓取 N 个 URL（重复正文与失败页也占预算，网络重试单独限制）。失败返回非零退出码，保存失败保留旧文件和索引。

**可选 Agent 模式**：Claude Code / Codex 经 stdio MCP → Relay → 获得逐站点授权的侧栏任务。侧栏须保持打开。配置见 [安装与联调](docs/INSTALL_BROWSER.md) 和 [Relay 说明](bridge/README.md)。本地抓取按钮只采集当前页；人工等待/匿名超时回退属于 Agent 任务。

安装包：[armory-browser-0.1.2.zip](dist/armory-browser-0.1.2.zip)（只包含扩展，不包含 Python 爬虫）。

## 这次修复

重试成功清理旧错误；失败退出码；有界爬取；大小写/Unicode 文件名冲突；整轮保存及索引原子提交；保留默认 CA 与显式叶证书信任；验收正文判据、报告隔离、业务数据校验与异常清理。默认严格验证 TLS，不以关闭验证代替修复。

## 验证边界

本地已实际跑过 CLI → 回环 HTTP → 文件、回环 HTTPS、MCP/HTTP/SQLite；两个公开测试站也已抓取并核对落盘内容。相关回归可在本仓库重新运行：

```sh
python3 tests/test_cli_live.py
python3 tests/test_user_engine_fixes.py
python3 tests/test_user_harvest_fixes.py
python3 tests/test_user_acceptance_fixes.py
python3 tests/test_tls_verification.py
python3 tests/integration_mcp_relay.py
node --test tests/test_extension.mjs tests/test_controller.mjs tests/test_extraction_quality.mjs tests/test_capture_contract.mjs tests/test_minimal_panel.mjs tests/test_local_capture.mjs
```

TLS 测试还需要 OpenSSL，额外传输后端测试需要对应可选依赖（如 curl_cffi）；JS 测试及 signer 需要 Node.js，店铺图片重建工具需要 Pillow。最小静态 CLI 不依赖这些组件。
模拟 DOM/Chrome 接口测试和浏览器事件夹具**不等于真实已安装扩展**。真实 Chrome 安装/登录协作、真实 Claude Code/Codex 调用和 Google 审核仍待验收。不是已上线的云服务，也不保证任意站点可采集。

原始素材、浏览器配置、凭据、运行日志与展示 demo 不在发布包内。只访问你有权访问的站点与数据。

[公开隐私政策](https://aji-q.github.io/armory-privacy/) · Jay Qin · jayqin04@gmail.com
