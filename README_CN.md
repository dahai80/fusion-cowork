[English](README.md) | [中文](README_CN.md)

---

<div align="center">
  <h1>🧹 Fusion-Cowork</h1>
  <p><strong>macOS 原生、本地优先、零代码桌面智能自动化平台</strong></p>
  <p><em>让 Mac 自己干活，本地 AI 全自动桌面办公</em></p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-brightgreen" alt="macOS">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="许可证">
  <img src="https://img.shields.io/badge/AI-MLX%20Native-orange" alt="MLX">
  <img src="https://img.shields.io/badge/离线优先-核心特性-important" alt="离线优先">
  <img src="https://img.shields.io/badge/状态-beta-yellow" alt="Beta">
  <img src="https://github.com/dahai80/fusion-cowork/actions/workflows/ci.yml/badge.svg" alt="CI">
</p>

---

## 📋 产品简介

**Fusion-Cowork** 是 [Fusion-MLX](https://github.com/fusion-mlx) 全栈 Apple Silicon 本地 AI 生态的三大旗舰核心产品之一，面向所有办公用户提供零代码桌面智能自动化能力。

### 生态层级定位

| 产品 | 定位 | 面向用户 |
|------|------|----------|
| **Fusion-Code** | 开发者编码智能体 | 程序员 |
| **Fusion-Agent-Studio** | 高级智能体工作流编排 | 开发者 / 架构师 |
| **Fusion-Cowork** 🎯 | 大众用户成品自动化工具 | **所有办公用户** |

> Studio 造流程、Desk 用流程、Code 写能力、KB 存知识、Hub 管模型

### 核心差异化优势

- ✅ **100% 本地离线** — 零文件上传、零隐私泄露、无埋点
- ✅ **macOS 原生深度适配** — 桌面、下载、文稿目录专项自动化
- ✅ **MLX 硬件加速** — Apple Silicon 原生推理，无需 GPU
- ✅ **AI 语义理解** — 理解文件内容，不只是文件名
- ✅ **零代码操作** — 一句话描述需求，自动生成完整流程
- ✅ **Fusion 全生态互通** — 与 Code / Studio / KB / Hub 无缝联动

---

## 🚀 快速开始

> **第一次用 fusion-cowork？** 跟着 **[入门指南](docs/guide.md)** 走 —— 场景化实战演练（整理桌面、一句话 AI 生成工作流、批量处理文档、自动化网站、定时任务、Claude Code MCP、团队协作、云端部署），含场景选择决策表和排错决策树。本 README 是完整的命令/节点参考手册；指南是上手坡道。

### 安装

```bash
# 克隆项目
git clone https://github.com/dahai80/fusion-cowork.git
cd fusion-cowork

# 安装依赖
pip install -e .

# 安装 AI 支持（可选，建议）
pip install -e ".[web]"

# 办公交付节点（xlsx_write / pptx_write，issue #151 #152）
pip install -e ".[office]"

# harness 内核 + plugins 生态（engine import 必需，issue #196）
# monorepo 内 extra 可解析（同仓 fusion-plugins-ecosystem 已装）。
# 独立克隆时 extra 不可解析（fusion-plugins-ecosystem 是 monorepo 同仓项目，
# 不在 PyPI）— 请改从 PyPI 装内核：
#   pip install fusion-harness
pip install -e ".[plugins]"

# 或使用 uv（更快）
uv venv --python 3.11
uv pip install -e ".[test]"
```

> 说明：裸装 `pip install -e ".[test]"` 只完成安装，跑完整测试套件还需要
> fusion-harness 内核 — 先执行 `pip install fusion-harness`
> （monorepo 内也可 `pip install -e ".[plugins]"`，同仓的
> `fusion-plugins-ecosystem` 已装好）。测试套件会 import
> `fusion_cowork.engine`，其 shim 需要 fusion-harness；缺包时收集期即报出
> 可执行的安装指引（`engine/_harness_guard.py`，issue #196）。

### 基础使用

```bash
# 查看可用模板
fusion-cowork template list

# 查看模板详情
fusion-cowork template show desktop_daily_cleanup

# 运行模板（预览模式）
fusion-cowork template run desktop_daily_cleanup --dry-run

# 运行模板（实际执行）
fusion-cowork template run desktop_daily_cleanup

# AI 一句话生成工作流
fusion-cowork ai generate "帮我把桌面所有 PDF 按主题分类归档"

# 检查 AI 服务状态
fusion-cowork ai status

# 查看系统信息
fusion-cowork system info

# 启动 MCP 服务 (stdio 模式，供 Claude Code 调用)
fusion-cowork mcp serve

# 启动 MCP 服务 (HTTP 模式)
fusion-cowork mcp serve --transport http --port 11438

# 启动 Desk RPC 服务 (供 Fusion-Studio GUI 调用)
fusion-cowork desk rpc

# 生命周期管理 (start.sh)
./start.sh start    # 启动 desk RPC 守护进程
./start.sh stop     # 优雅停止
./start.sh restart  # 停止 + 启动
./start.sh status   # PID、Socket、内存、运行时间
./start.sh log [-f] # 查看日志 (-f 实时跟踪)
./start.sh doctor   # 健康检查 (venv、CLI、Socket、上游服务)
./start.sh clean    # 轮转日志、清理 __pycache__
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FUSION_CLIENT_KEY` | _(未设)_ | **fusion-gateway 客户端 api key**（取自 fusion-gateway `config.yaml` 中 `auth.api_keys[].key`）。**不是** fusion-mlx `settings.json` 的 `auth.api_key` —— gateway 与 mlx 是两套独立鉴权。经 `fusion_core.config.client_key()` 单点解析 (issue #204);legacy 别名 `FUSION_MLX_API_KEY`/`MLX_API_KEY` 由 fusion-core 兼容 (弃用告警);`FUSION_PROFILE=prod` 缺 key 直接报错。 |
| `FUSION_MLX_URL` | `http://localhost:11432/v1` | fusion-mlx 基础 URL (经 fusion-gateway netlayer) |
| `FUSION_MLX_MODEL` | _(未设)_ | NL 工作流生成 (`NLWorkflowGenerator`) 默认 chat 模型。显式指定一个支持 chat 的模型 id,避免误选 `image_gen`/`video_gen` 模型 (issue #85) |
| `FUSION_RAG_URL` | `http://localhost:11436` | fusion-rag (fusion-kb) 基础 URL |

> **鉴权说明**: fusion-cowork 通过 fusion-gateway(:11432) 调 fusion-mlx,存在**两套独立鉴权**:
> 1. **gateway client key** —— 客户端请求 gateway 时用,即本变量 `FUSION_CLIENT_KEY`(取自 gateway `config.yaml` 的 `auth.api_keys[].key`;legacy 别名 `FUSION_MLX_API_KEY`/`MLX_API_KEY` 由 fusion-core 兼容,已弃用)
> 2. **mlx backend key** —— gateway 转发到 mlx 时用,配置在 gateway `config.yaml` 的 `backends.fusion-mlx.api_key`,客户端无需关心
>
> 常见 401 错误:把 mlx 的 key 填进了 `FUSION_CLIENT_KEY`。应填 gateway 的 client key。

```bash
# 示例：设置 API 密钥为 fusion-gateway 的 client key（来自 config.yaml 的 auth.api_keys）
export FUSION_CLIENT_KEY="fg-admin-key"
fusion-cowork desk rpc

# 会话管理
fusion-cowork session list
fusion-cowork session show <session_id>
fusion-cowork session fork <session_id> --from-step 2
fusion-cowork session cleanup --days 30

# 权限管理
fusion-cowork permission level <manual|auto|plan|bypass>
fusion-cowork permission approve <tool_name> --scope <scope>
fusion-cowork permission deny <tool_name> --scope <scope>
fusion-cowork permission list

# 功能对比基准
fusion-cowork benchmark report --format markdown|html|json [-o report.md]
fusion-cowork benchmark run --node file_input --node shell_exec --repeats 3

# 插件管理 (M3)
fusion-cowork plugin list
fusion-cowork plugin install /path/to/plugin
fusion-cowork plugin load <name>
fusion-cowork plugin uninstall <name>

# 技能管理 (M3)
fusion-cowork skill list
fusion-cowork skill run /cleanup
fusion-cowork skill search "clean"

# 导出对账 — 预算 vs 导出目录差异对账（issue #151）
fusion-cowork skill run /spreadsheet-recon "~/fin/budget.xlsx ~/fin/exports 10"

# Markdown 技能包 — frontmatter 参数在 Web UI 渲染为表单（issue #155）
fusion-cowork skill packages
fusion-cowork skill run-package /weekly-readout --params '{"days": 7}'
fusion-cowork skill run-package /contract-review --params '{"contract_path": "~/contracts/a.md", "kb_id": "playbook"}'
# 包格式: YAML frontmatter (name/params/handler/output_template) + markdown 指令正文。
# 共享格式 (issue #167): JSON Schema parameters + allowed-tools + version 同时被
# fusion-cowork 与 fusion-agent-studio 识别, 一个包两侧可用 — 详见 docs/skill-packages.md。
# 内置包在 fusion_cowork/skills/packages/；自写包放入 ~/.fusion-cowork/skills/，
# 或用 FUSION_COWORK_SKILLS_DIRS=目录1:目录2 指向共享目录。

# Chrome CDP — 远程浏览器控制 (M3)
fusion-cowork cdp navigate https://example.com
fusion-cowork cdp snapshot
fusion-cowork cdp click 42
fusion-cowork cdp fill --selector "#search" --value "hello"
fusion-cowork cdp screenshot --save ~/Desktop/shot.png
fusion-cowork cdp evaluate "document.title"

# 托管浏览器（issue #153）— 自动拉起独立 profile 的 Chromium 并分配 CDP 端口，
# 无需手工配置 --remote-debugging-port。工作流节点 `managed_browser`
# (action launch/close/status, import_cookies=<Netscape cookies.txt>)。
# `fusion-cowork web serve` 暴露 POST /api/browser/launch|close 与
# GET /api/browser/status|screenshot（Web 侧栏实时截图）。
# 可执行文件发现可用 FUSION_COWORK_CHROMIUM=<path> 覆盖 Chrome/Chromium/Edge/Brave 查找。

# fusion-browser 引擎（issue #162）— 托管拉起 fusion-browser UDS 引擎，每运行独立配置
# (绝不写入 ~/.fusion-browser)，每运行持久 profile，登录态导入返回诚实计数，面板直连 UDS
# 截图流。工作流节点 `fusion_browser` (action launch/create_session/import_cookies/execute/
# screenshot/finish/close)。Web 侧: POST /api/fusion-browser/launch|session|import-cookies|
# execute|finish|close 与 GET /api/fusion-browser/status|screenshot|stream (SSE)。
# 二进制覆盖: FUSION_BROWSER_BIN。

# 协作空间 (M6+M7)
fusion-cowork space create --name "Project Alpha" --owner-id user1
fusion-cowork space list
fusion-cowork space get <space_id>
fusion-cowork space archive <space_id>
fusion-cowork space member list <space_id>
fusion-cowork space member invite <space_id> --inviter-id user1 --role member
fusion-cowork space member join <invite_code> --user-id user2
fusion-cowork space member remove <space_id> --user-id user3 --operator-id user1
fusion-cowork space chat <space_id> --user user1 --agent agent1
fusion-cowork space knowledge bind <space_id> --operator user1
fusion-cowork space knowledge status <space_id>
fusion-cowork space knowledge upload <space_id> doc.pdf --operator user1
fusion-cowork space knowledge search <space_id> "query text" --top-k 5
fusion-cowork space knowledge unbind <space_id> --operator user1
```

---

## 🎯 内置模板（10 个开箱即用）

| 模板 | 分类 | 说明 | 需要 AI |
|------|------|------|----------|
| 🧹 **桌面每日规整** | 桌面清理 | 按类型自动规整桌面文件 | ❌ |
| 📥 **下载文件夹自动归档** | 文件整理 | 按类型归档、去重、整理下载文件夹 | ❌ |
| 📝 **文档批量 AI 摘要** | AI 处理 | 批量总结文档生成汇总报告 | ✅ |
| 🏷️ **AI 智能文件分类归档** | AI 处理 | AI 根据内容语义自动分类归档 | ✅ |
| ✏️ **AI 批量智能重命名** | AI 处理 | AI 根据内容生成规范文件名 | ✅ |
| 💾 **磁盘空间清理** | 系统维护 | 扫描清理缓存和垃圾文件 | ❌ |
| 📂 **项目资料一键归集** | 文件整理 | 按类型归集项目资料 | ❌ |
| 🔍 **重复文件扫描清理** | 系统维护 | 扫描并清理重复文件 | ❌ |
| 📋 **日志文件自动清洗** | 系统维护 | 扫描清理日志文件 | ❌ |
| 🖼️ **图片批量重命名分类** | 文件整理 | 批量重命名图片并分类 | ❌ |

### 行业模板 (V0.3)

| 模板 | 行业 | 说明 |
|------|------|------|
| 🎨 **设计资产整理** | 设计 | 自动分类 PSD/AI/Sketch 文件 |
| 🖼️ **设计批量导出** | 设计 | 批量导出设计资产 |
| 💻 **项目代码清理** | 开发 | 清理缓存、pycache、node_modules |
| 🔍 **日志分析器** | 开发 | AI 驱动日志分析 |
| 🚀 **项目脚手架** | 开发 | 自动创建项目结构 |
| 📊 **CSV 数据清洗** | 数据 | 去重、规范化 CSV |
| 🔄 **格式转换器** | 数据 | 批量 CSV↔JSON↔Excel |
| 🖥️ **系统巡检** | 运维 | 收集系统健康报告 |
| 💾 **备份管理器** | 运维 | 定时文件备份 |
| ⚠️ **磁盘告警** | 运维 | 监控磁盘用量、自动清理 |

---

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────────────┐
│                     UI 层                              │
│   CLI (click)  │  Web UI (FastAPI)  │  macOS (.app)   │
├─────────────────────────────────────────────────────┤
│                  流程引擎层                             │
│   WorkflowEngine  │  TaskScheduler  │  NodeRegistry   │
│   (DAG, n8n 启发)  │  (APScheduler)  │  (47 个节点)    │
├─────────────────────────────────────────────────────┤
│                   AI 能力层                             │
│   FusionMLXClient  │  NLWorkflowGenerator             │
│   (HTTP → fusion-mlx)  │  (自然语言 → 工作流)          │
│   KBClient (HTTP → Fusion-KB)                        │
├─────────────────────────────────────────────────────┤
│                 系统能力层                              │
│   macOS 节点 (AppleScript / osascript)                │
│   文件操作  │  桌面整理  │  磁盘清理  │  通用工具       │
├─────────────────────────────────────────────────────┤
│              协作空间层 🆕                              │
│   SpaceStore (SQLite WAL)  │  SpaceService (CRUD)     │
│   SpacePermission (4级)     │  SpaceMemberService     │
│   SpaceChatService (SSE)   │  SpaceKBService (RAG)    │
│   SharedContext (节点)      │  SpaceAPI (REST+SSE)     │
└─────────────────────────────────────────────────────┘
```

### 节点类型（72 个内置节点，7 大分类）

| 分类 | 数量 | 节点 |
|------|------|------|
| `macos_system` | 13 | 桌面清理、下载整理、磁盘清理、文件监听、屏幕截图、剪贴板、通知、应用生命周期、OCR、**鼠标移动、鼠标点击、键盘输入、键盘快捷键、Computer Use 循环** 🆕 |
| `ai_processing` | 4 | AI 分类、AI 摘要、AI 重命名、AI 视觉分析 |
| `tool` | 38 | Shell 命令、Python REPL、Web 搜索、获取网页、文件编辑、浏览器打开/提取/自动化、**托管浏览器** (issue #153)、**fusion-browser 引擎** (issue #162)、CDP 浏览器节点族（导航、快照、点击、填写、截图、执行JS、模拟、网络、控制台等）、Push/LSP/Worktree |
| `file_operation` | 6 | 文件分类、批量重命名、复制、移动、删除、查找 |
| `io` | 4 | 文件输入、文件输出、**xlsx_write** (issue #151)、**pptx_write** (issue #152) |
| `logic` | 3 | 条件过滤、循环处理、数据合并 |
| `fusion_ecosystem` | 4 | Trainer（Fusion-Trainer 互通）、记忆提交/记忆检索（Fusion-Memory 互通）、**Studio 运行**（Fusion-Agent-Studio 互通, issue #133） |

### Claude Cowork 对等能力 (V0.2) 🆕

| 能力 | 状态 | 实现 |
|------|------|------|
| 屏幕截图与桌面视图 | ✅ | `ScreenCaptureNode` — 全屏/选区/窗口截图 |
| 剪贴板读写 | ✅ | `ClipboardNode` — pbpaste/pbcopy |
| 系统通知 | ✅ | `NotificationNode` — macOS 通知中心 |
| macOS 应用生命周期 | ✅ | `AppLifecycleNode` — 启动/退出/激活/列表 |
| OCR / 屏幕文字识别 | ✅ | `OCRNode` — Vision + fusion-mlx |
| MCP 协议服务 | ✅ | `MCPServer` — 16 个工具供 Claude Desktop/Code 调用 |

### 生态互通

| 组件 | 协议 | 用途 |
|------|------|------|
| **fusion-mlx** | HTTP API (经 fusion-gateway :11432) | LLM 推理、文本生成、嵌入 |
| **Fusion-RAG** | HTTP API (端口 11436) | 知识库语义检索、RAG |
| **Fusion-Code** | 自动生成脚本 | 复杂逻辑自动执行 |
| **Agent-Studio** | HTTP API (:11442) + 工作流导入 | `studio_run` 节点提交 graph/agent 运行；调用高级编排的工作流 |
| **Model-Hub** | 模型调度 | 自动选择最优本地模型 |

### Run 级结构化执行日志 (v0.5.19, issue #120)

`WorkflowEngine` 输出 run 级结构化日志 — 失败的运行只看日志即可定位, `grep "[<run_id>]"` 重建完整执行时间线:

```
[<run_id>] run start: workflow=<名称> nodes=<n>
[<run_id>] step start: node=<节点类型> node_id=<id>
[<run_id>] step end:   node=<节点类型> verdict=<success|gated|blocked|failed> duration=<秒> params=<脱敏> error=<...>
[<run_id>] run end:    outcome=<状态> total=<秒> steps=<n>
```

- **verdict ≠ 状态** — 节点输出标记 (`blocked=true`、`passed=false`、`gated`/`requires_approval`) 区分业务门控 (L2 硬规则拦截、L3 审批门) 与错误; 权限拒绝步骤记 `verdict=gated`。
- **params 脱敏** — 默认仅保留标量, 字符串截断 200 字符, 复杂值汇总 (`<dict:N>`), 敏感键名 (`*key*`/`*token*`/`*password*`/...) 置 `***`, 最多 20 键。
- **日志级别** — step start/end 用 INFO, 失败与门控用 WARNING, 引擎内部错误用 ERROR。`AgentOrchestrator` 任务执行遵循同一 `[<task_id>]` 约定。

### 验收批次修复 (v0.5.20, issue #122–#127)

来自 2026-10-20 五仓 issue 验收的 6 个问题, 一批修复:

- **#122 (P1) — 旧库上 Space store 初始化崩溃**: tenant 索引移出 `_SCHEMA_SQL`, 改为 `MigrationRunner.run_pending()` (v1 先补 `tenant_id` 列) 之后执行 `_TENANT_INDEX_SQL`。pre-v0.4 旧库不再报 `no such column: tenant_id`, 升级安装上 `/api/space/*` 恢复可用。
- **#123 (P2) — `/api/deliver` 接线**: `web_serve` 在 `FUSION_COWORK_CROSS_SYNC=1` 时注入 `CrossDeviceSync` (`FUSION_COWORK_SYNC_HOST/PORT/TOKEN/CERT/KEY` 配置; 默认 off → 诚实 503 不变)。`server/sync.py::cross_sync_from_env()` 为可测接缝。
- **#124 (P2) — 本地 CONFIRM 审批**: guard 关闭时高风险拒绝现写入 `PermissionManager._pending_local_approvals`; `list_pending()` 合并 guard + local (带 `source: guard|local`); `confirm_guard()` 无需 guard 在线即可批准本地条目。`/api/permissions/pending` 与 `/api/mobile/overview` 同时展示两类来源 — 默认姿态下 Approvals 页面不再永远为空。
- **#125 (P2) — manifest upstream/api_key**: `web/manifest.json` 声明 `upstream` (`http://127.0.0.1:11461`) + `api_key_env`; `shell_registry.load_manifest()` 注册时从该 env 解析 `api_key`, 并支持 `FUSION_COWORK_WEB_UPSTREAM` 覆盖。网关动态代理 (#232 契约) 可解析本模块。
- **#126 (P3) — 模型目录只回 chat**: `/api/models` 默认只回 chat 可用模型 (`engine_type` 不在非 chat 集合); `?all=1` 返回原始列表; `total` 携带原始计数。`_is_chat_model` 与 `NLWorkflowGenerator._pick_chat_model` 共用。
- **#127 (P3) — vendor 同步 + web 测试**: vendor `@fusion/web-sdk`/`web-ui` 与上游字节一致 (含 #19/#20 修复); `web/tests/sdk.test.ts` 新增首个 `bun test` 套件, 覆盖 #96 runtime-key 接缝 + #20 content-type 契约 (`bun run test`)。

### 节点执行器与并行调度 (v0.5.40, issue #189)

- **#189 — `NodeExecutor` 协议** (`fusion_cowork/engine/executors.py`): 引擎不再直连 `node.execute()`, 而是按节点解析执行器 (`executor_for_node`)。`SimpleExecutor` (默认) 原样转发, 现有工作流零行为变化; `AgentExecutor` 把任务文本交给 fusion-agent-studio 的 agent, 并回传真实多轮循环 (`turns`/`tool_calls`/`tokens`)。
- **#189 — 逐节点执行器配置**: `NodeConfig` 新增 `executor`/`agent`/`tools`/`model`/`max_turns`/`budget_tokens` (均由 `to_dict()`/`from_dict()` 序列化), 单条 DAG 可混合确定性节点与 agent 节点。未知执行器名记 WARNING 后回退 `simple`。
- **#189 — agent 通道**: UDS `subagent.run`/`subagent.transcript` (`/tmp/fusion-studio.sock`, `FUSION_AGENT_STUDIO_SOCKET`) 支持全部逐节点覆盖; HTTP 通道 (`FUSION_AGENT_STUDIO_URL`, 默认 `:11442`) 不支持, 因此显式记降级 WARNING 而非假装生效。daemon 缺失 → 节点可见失败, 绝不静默降级为普通 LLM 调用。
- **#189 — ready-set 波次调度**: 前驱已完成的节点同波执行, 受并发度约束。解析顺序: `execute(concurrency=N)` > `FUSION_COWORK_MAX_CONCURRENCY` > `1`。并发 `1` 与历史串行拓扑循环完全等价。`start.sh` 导出 `FUSION_COWORK_MAX_CONCURRENCY=4`, 服务进程默认并行, 库默认仍串行。
- **#189 — 并发下的确定性**: 步骤统一按拓扑序 (而非完成序) 落盘后再通知进度, REST/SSE/步骤日志在串行与并行下完全一致。改向指令 (`set_params`/`insert_node`/`skip_remaining`) 在波次边界针对首个未执行节点生效; HITL 审批经 `_RunState.hitl_lock` 按 run 串行。
- **#189 — 校验策略**: 无连线节点不再是校验错误 — 记 `工作流含无连线节点, 将作为独立分支执行` 后作为独立分支运行, 这正是"N 个独立节点并行"得以表达的前提。
- **#189 — 可观测性**: `WorkflowStep.executor`、`WorkflowExecution.concurrency`/`executors`, run start/end 日志带 `concurrency=... executors={'simple': 2, 'agent': 1}`, 最终 summary 同步给出执行器混比。

测试: `tests/test_issue_189_node_executor.py` (33 例 + 1 例 live-AS 跳过: 波次并发墙钟/重叠/峰值、env 解析、配置往返、UDS 成功/partial/失败/transcript 容错、HTTP 完成/失败/超时/覆盖降级、混合 DAG 端到端、agent 失败语义、跨 agent 节点断点续跑)。文档: `docs/node-executors.md`。

### MCP 注册表审计降级警告 (v0.5.48, issue #214)

- **#214 — 降级模式可见**: `fusion_core` 不可导入时 (CI 裸装), `mcp_client` 注册表检查原先只留一条 DEBUG 日志就静默跳过 — 审计悄悄消失。现在跳过分支发 WARNING (`注册表审计降级跳过`), 携带目标 `url` + `tool`, 每进程只发一次 (避免 CI 循环刷屏)。完整 fusion 部署中 fusion-core 是硬依赖, 该警告永不触发。`docs/mcp-registry.md` 已记录降级行为。

### MCP 客户端 JSON 字符串参数 (v0.5.47, issue #211)

- **#211 — `mcp_client` 接受 JSON 字符串 `arguments`**: 本节点输出恒为文本 (`data["result"]`), 因此两个 `mcp_client` 之间的连边 `{"source_output": "result", "target_input": "arguments"}` 会把字符串传给下游, 下游在任何 HTTP 调用前就报 `arguments 须为 JSON 对象`, 导致 `mcp_client → mcp_client` 无法串联。现在字符串参数 (来自 inputs 或 params) 先 trim 再 `json.loads` 解析: 合法 JSON 对象直接使用, 非法 JSON 报 `JSON 字符串解析失败`, 合法但非对象的 JSON 保持原错误; 非字符串非对象的行为不变。

### HTTP 请求节点 + Run 归因 + MCP 注册表检查 (v0.5.46, issues #206–#210)

- **#206 — 通用 `http_request` 节点** (`nodes/tools/http_request_node.py`): 一节点一次 HTTP 请求 (`method`/`url`/`headers`/`body`/`timeout`), 外部应用可在 DAG 里直接回调自家 REST API, 无需跑 MCP 服务器。body/headers 支持 `${dotted.path}` 模板 (从节点输入解析, 含 run 级 `inputs` 注入); 未命中保留字面量并记 WARNING。响应 JSON 进 step 输出 (`status_code`/`json`/`text`/`headers`) 供下游消费; 非 2xx 映射为节点失败, run 状态对账成立。HIGH_RISK 节点 + `url:` scope 授权 (同 `mcp_client`); 面向本机/内网自家服务, 不做 SSRF 私网拦截。
- **#207 — per-run 来源/标签**: `POST /api/runs` 新增可选 `source` (字符串) 与 `labels` (字符串到字符串对象), 类型校验 (非法则 400), 落 run 记录并在 `GET /api/runs` + `GET /api/runs/{id}` + 事件广播透出; `PushEgress` 的 `run_finished`/`approval_requested` 载荷同步携带 (审批事件用 `run_source`/`run_labels` 键), webhook 接收方可按来源应用路由。全部增量, 老调用方零变化。
- **#209 — HTTP 传 per-run inputs**: `POST /api/runs` 新增可选 `inputs` (别名 `initial_input`), 以 `message_input` 语义转发到 `RunRegistry.start` — 与邮箱派发路径一致, 首节点直接拿到运行参数, 不再需要把参数烤进一次性工作流定义副本。非 dict 一律 400, 不再静默丢弃。
- **#210 — MCP 服务器注册表检查**: `mcp_client` 节点在 `initialize` 握手前调用 `fusion_core.mcp_registry.check_approval(url, tool_scope=tool)` (需 fusion-core >= 0.9.0)。`denied`/`unknown` 记 WARNING (含 verdict reason + correlation id, 即 `fc_` 观测 trace id) 但不阻断 — HIGH_RISK `url:` scope 人工审批仍是执法点。注册表不可读或 fusion-core 缺席 (CI 裸装) 时跳过检查, 零行为变化。
- **#208 — 消费侧注册表与凭证约定文档**: 新增 [`docs/mcp-registry.md`](docs/mcp-registry.md) — 受理服务器清单 (`architecture/mcp-server-registry.json`, 解析顺序 + mtime 缓存)、按 scope 人工审批、消费侧凭证发放 (`identity_issuance` / `env_ref`, "不自带密钥", Sheets 服务器示例)、warn 级执法、correlation id 审计契约。

测试: `tests/test_issue_206_http_request_node.py`、`tests/test_issue_207_run_labels.py`、`tests/test_issue_209_runs_inputs.py`、`tests/test_issue_210_mcp_registry_check.py`。

### 运行状态持久恢复 (v0.5.38, issue #191)

- **#191 — `RunStateStore`** (`fusion_cowork/engine/run_persistence.py`): 运行控制状态落 SQLite `<store_root>/run_states.db` (默认 `~/.fusion-cowork/web/run_states.db`)。每个**活跃** run (`running`/`paused`) 一行; 终态行 run 结束即删除, 加载时发现的终态残留行以 WARNING 清理。schema 版本走 `PRAGMA user_version` — 打开更新版本 fusion-cowork 写的库直接 `RuntimeError` 启动失败, 绝不静默降级读取。
- **#191 — 事件即检查点**: 每个引擎事件 (`RunRegistry._on_event`) 持久化完整记录 — 工作流定义、步骤快照、排队中的改向指令、状态、`paused_reason`。持久化失败仅 WARN; 持久化是旁路, 永不阻塞执行。
- **#191 — 唯一恢复路径**: `RunRegistry.recover_persisted()` 在 FastAPI startup 执行。每个活跃行重建为 `paused`, 引擎任务以 `start_paused=True` + `resume_steps=<快照>` 重启 — 与进程内 pause/resume 同一条 resume 路径, 步骤级进度和控制状态经同一代码路径恢复。持久化的改向指令重放进新引擎队列, resume 后首个节点边界生效。无法解析的行 ERROR 记录后删除, 绝不静默重试。
- **#191 — crash 判定响亮**: 持久化状态为 `running` 的行 = 上次会话未清理即退出 (`kill -9`、崩溃)。恢复记 WARNING, 以 `paused` + `paused_reason="crash"` 重建。恢复的 run **绝不自动续跑** — 等人工 `POST /api/runs/{id}/resume` 或新的 HITL 审批。
- **#191 — 向后兼容**: 未注入 `run_store` 的 `RunRegistry` 保持纯内存 (零持久化); `engine.execute()` 仅在设置时传 `resume_steps`/`start_paused`, 注入的旧引擎不受影响。恢复的 run 跳过 memory-bridge 重复注入 (首步快照已含原始上下文)。详见 `docs/run-recovery.md`。

测试: `tests/test_issue_191_durable_run_state.py` (8 例: store 往返、终态残留响亮清理、schema 版本门、kill -9 崩溃恢复为 paused + HITL 重入 + 快照跳过、恢复后指令重放、手工暂停行按 manual 恢复、坏行响亮废弃、`create_web_app` store 接线)。

### HITL 默认开启 + 声明治理 (v0.5.37, issues #187/#188)

- **#187 — HITL 暂停等待审批改为默认开启**: `FUSION_COWORK_HITL_PAUSE` 语义翻转 — 除非显式设为 `0`/`false`/`off`/`no` (headless deny-fast, 即 v0.5.37 之前的行为), 高风险节点默认在节点边界暂停等人工审批。**无人值守部署迁移**: 设 `FUSION_COWORK_HITL_PAUSE=0` 保持 deny-fast; 其他值或缺省即暂停等待。
- **#187 — 有界等待**: `FUSION_COWORK_HITL_TIMEOUT` (默认 `1800` 秒, 支持 `s`/`m`/`h` 后缀) 限制每次 HITL 等待。超时自动拒绝被闸节点 (绝不静默放行), 滞留待审批项从队列清理, 触发 `hitl_timeout` 通知 hook, run 走既有 DENIED 路径。
- **#187 — `completed_with_denials` 终态**: 被拒节点带 `continue_on_error=true` 的 run 现以 `completed_with_denials` 收尾 (不再误报纯 success) — 交付可见"完成了但有否决" (web `statusKind` 映射为 warning)。
- **#187 — 一次性批准**: `POST /api/permissions/pending/{action_id}/confirm` 接受 `"once": true` — 出队待审批项并唤醒暂停中的 run (`resolve_hitl(..., once=True)` 一次性放行, 不落权限规则)。缺省仍经 `confirm_guard` 落规则。
- **#187 — web 审批卡**: run 详情在 `hitl_pending` 期间显示等待审批卡 — 节点名、脱敏参数、按服务端 `hitl_deadline` 的实时倒计时, 三操作: 本次允许 / 总是允许 (落规则) / 拒绝。待审批状态在服务端, 页面刷新不丢; 2s 轮询保持实时。
- **#188 — 虚假并行声明清理**: `engine/workflow.py` 模块/类 docstring 不再声称并行执行 — 主循环为拓扑序串行 (并如实标注)。能力声明带 `CLAIM: verified by tests/<file>.py` 标签, `scripts/check_claims.py` (接入 CI lint job) 对悬空引用或 `engine/workflow.py` CLAIM 覆盖不足直接失败。

测试: `tests/test_issue_187_hitl_default.py` (21 例: env 语义翻转、`_parse_hitl_timeout` 单元、超时自动拒绝+队列清理+截止时间弹出、`completed_with_denials`、非高危不受影响、一次性批准不落规则往返、discard 助手、web confirm-once + `hitl_deadline` + 刷新持久); `tests/test_issue_181_hitl_pause.py` 默认断言更新; `tests/conftest.py` 套件整体 headless。

### MCP 客户端节点 (v0.5.36, issue #185)

- **一等 `mcp_client` 节点** (`nodes/ecosystem/mcp_client_node.py`): 工作流可经 Streamable HTTP 调用外部 MCP 工具服务器 — 一节点一次工具调用。节点执行完整三段式 (`initialize` 握手 → `notifications/initialized` → `tools/call`), 复用服务端下发的 `Mcp-Session-Id` 请求头, 同时解析 JSON 与 SSE 帧 (`text/event-stream`) 的 JSON-RPC 响应, 透传自定义 `headers` (鉴权), `isError` 结果如实报节点失败。补齐 cowork 作为 MCP 服务端/客户端的不对称, 移除日常 DAG 里的 `python_repl` 逃生通道。
- **URL 粒度权限**: `mcp_client` 为 HIGH_RISK 节点 (远程工具执行面), `Permission.matches` 新增 `url:` 前缀 scope — `approve("mcp_client", scope="url:http://127.0.0.1:11483/**")` 只授权单个服务, 不放开任意目标, 也不弱化整体权限模型。中文别名: `MCP工具` / `MCP调用` / `调用MCP`。
- 经 `import_all_nodes()` 注册 (现 13 个节点模块); 兄弟服务调用不再绕 `fetch_url` 的环回 SSRF 拦截 — 本节点即为此设计的正路。

测试: `tests/test_issue_185_mcp_client_node.py` (11 例: 完整协议序列与会话头断言、JSON+SSE 响应解析、isError 失败路径、连接失败可见、缺参/非法参数快速失败、自定义头透传、注册、HIGH_RISK 列表、url-scope 权限匹配单元+管理器级、未授权拒绝)。

### 引擎可靠性批次 (v0.5.35, issues #180/#181/#182)

- **#180 — python_repl 多语句修复**: 子进程包装脚本读了两次 stdin — `eval(sys.stdin.read())` 先耗尽管道, `SyntaxError` 分支再读得到空串, 多语句代码静默不执行还报 success。现改为 stdin 只读一次存 `_src`, 先 eval 编译、`SyntaxError` 再 exec 编译; 用户语法/运行时错误如实报节点失败, 不再静默空成功。
- **#182 — 重试可观测**: `WorkflowStep` 新增 `attempt` (1-based 最终尝试序号) 与 `attempt_errors` (每次失败尝试一条); 两者经 run steps API (`_step_dict`) 暴露。`NodeConfig` 新增 opt-in `retry_on_business_failure` (默认关) — 业务 FAILED 结果仅在节点显式开启后参与引擎重试; 异常/超时重试行为不变。`to_dict`/`from_dict` 往返新字段。
- **#181 — HITL 暂停等待审批**: 引擎 opt-in 模式 (构造参数 `hitl_pause=True` 或 `FUSION_COWORK_HITL_PAUSE=1`) — 高风险节点落入本地待审批队列时, run 暂停在节点边界 (复用 W8 暂停事件; `cancel()` 仍可唤醒), 不再 deny-fast 失败。`POST /api/permissions/pending/{action_id}/confirm` 现读取待审批条目的 `execution_id`, 规则落盘后唤醒暂停中的 run (`engine.resolve_hitl`), 原地只重跑被闸节点 (输入不变) — 批准的 run 原地续跑 (不重放已执行节点), 拒绝走既有 DENIED/补偿路径。等待期间 `GET /api/runs/{id}` 报 `hitl_pending`。guard 侧 (fusion-guard L3) 待审批无 execution_id, 保持 deny-fast。
- **测试隔离加固**: web-api 鉴权测试现打桩 `_module_key()` — 本机已签发的网关模块密钥 (`~/.fusion-cowork/web_module_key`) 不再把"未配密钥"契约测试翻成 401。

测试: `tests/test_issue_180_repl_stdin.py` (6 例)、`tests/test_issue_182_retry_observability.py` (7 例)、`tests/test_issue_181_hitl_pause.py` (7 例, 含完整 web confirm 路由接线)。

### 心跳携带 manifest + 跟随 shell 主题 (v0.5.34, issues #176/#177)

- **#177 — 心跳携带 manifest**: fusion-gateway #284 (`handleShellManifestHeartbeat`) 接受可选 manifest body — 带 id 的 body 触发 `HeartbeatWithManifest`, 收敛陈旧 ext 字段 (pages/searchProviders/notifications); 空 body 仍是纯保活 ping。模块侧现采纳该契约: `_registry_loop_body` 每拍重读磁盘 `web/manifest.json` 并作为心跳 body POST, 磁盘编辑 (增删页面) 一个心跳周期内收敛到网关注册表, 无需重启。重读失败 (文件缺失/损坏) 回退注册时版本, 保活不中断; fm- 密钥鉴权路径 (401 → 重注册) 不变。
- **#176 — iframe 跟随 shell 主题切换**: 新增 `web/src/theme.ts` — 嵌入模式订阅 `sdk.theme.onThemeChange` (shell 发 `{type:"event", event:"theme"}` 且 iframe 加载时重推, 无闪变), 切换 `html.light`/`html.dark` (tokens.css 类调色板); 独立模式初始主题依次取 `localStorage["fusion.shell.theme"]`、`prefers-color-scheme`、默认 dark, 并在每次切换后回写。`main.tsx` 首帧渲染前接线。

测试: `tests/test_issue_177_registry_heartbeat.py` (4 例: 心跳体携带 manifest、磁盘编辑一拍内收敛、重读失败回退注册版本、401 重注册臂不破坏); `web/tests/theme.test.ts` (6 例: light/dark 类切换、存储偏好/脏值/prefers-color-scheme/无存储的初始主题链)。

### 签发密钥提取 + 模型轮询修复 (v0.5.33, issues #172/#173/#174)

- **#173/#174 — 网关签发密钥从未落盘**: fusion-gateway K1 签发响应 (`writeShellKeyResponse`) 明文模块密钥在 `raw_key` 字段, 而 `_extract_issued_key()` 只找 `api_key`/`issued_key`/`key`/`module_key` — 一次性 `fm-` 密钥被静默丢弃, `~/.fusion-cowork/web_module_key` 从未写入。现在 `raw_key` 为首选项 (顶层 + `data` 嵌套); 旧字段名保留兼容旧/自定义网关。心跳按设计改用签发密钥鉴权。
- **#172 — `/api/models` 503 轮询风暴**: 模型客户端不可用 (冷缓存/上游离线/未注入客户端) 时, 前端固定间隔轮询对裸 503 每几秒重试一次, shell 页签控制台刷屏。路由现返回诚实 `200` 信封 — `{"models": [], "count": 0, "stale": false, "unavailable": true}` — 轮询拿到成功响应即渲染空态, 不再重试。#147 stale 缓存路径 (成功过一次后带 `stale: true` 返回缓存) 不变; `web/src/api.ts` 类型补充 `unavailable` 字段。

测试: `tests/test_issue_172_173_174_registry_models.py` (6 例: `raw_key` 顶层/嵌套/优先级提取 + 网关真实响应形状经注册循环落盘, 缺客户端 unavailable 信封, `?all=1` 信封形状); 更新 `tests/test_issue_146_147_web_auth_models.py` 与 `tests/test_web_p3.py` 契约。

### 扩展控制台：Skills + Knowledge (v0.5.32, issue #170)

迁移 f-ecommerce 扩展控制台（E2，insight/f-ecommerce-migration-1022.md）— Skills 与 Knowledge 两个页签，布局与交互不变，数据源端点换到本模块：

- **Skills 页签** — JSON 用户 skill 包（`~/.fusion-cowork/skills/<name>/skill.json`，handler 为 `workflow|node|script`），支持 校验并安装 / 测试运行 / 移除。安装经 cowork 自有持久化 API（`skills/persistence.py`）落盘，并注册进共享 `SkillRegistry`（能力 `skill.<name>`，MCP `skill_run` 可调）；包 handler 现按安装目录定位脚本包。脚本包（任意代码）仅管理员：本机操作员（未配鉴权）或静态 `FUSION_COWORK_WEB_API_KEY` 可装，网关签发 `fm-` 模块密钥一律拒绝。
- **Knowledge 页签** — fusion-rag 控制台，复用 SpaceKB 的 `KBClient` 绑定（`:11436`，`FUSION_RAG_URL` 覆盖）：健康标记、KB 创建并接线、可过滤+分页的 KB 列表（含文档数）、文档上传摄取（`.md`/`.txt`，10MB 上限，暂存 `~/.fusion-cowork/web/rag_uploads/`）、检索面板（只回引用相关字段 `text`/`doc_path`/`score`）。接线状态落 `~/.fusion-cowork/web/rag_wired.json`，呈现为 `knowledge.<kb_id>.search` 能力；断开时报告停用的能力 id。
- **Web** — 新 `#/extensions` 页（`ExtensionsPage.tsx`，`web/manifest.json` 页项）：页签栏 + 表格 + 安装/测试/KB 面板对齐源页面；`api.ts` 增加 `/api/extensions/*` 客户端面。

测试：`tests/test_issue_170_extensions_console.py`（22 例：包校验/安装/注册/脚本管理门/测试运行诚实信封/移除，RAG health/过滤/分页/接线/文档/上传/检索，KBClient 用离线替身）。

### 共享技能包格式 + 网关签发模块密钥 (v0.5.31, issues #167/#168)

- **共享 markdown 技能包格式 (#167)** — 一个包同时服务 fusion-cowork 与 fusion-agent-studio。`parse_markdown` 接受共享 frontmatter: JSON Schema `parameters`/`arguments` (`properties`/`required`, 映射为 cowork 表单字段, 运行时校验 `enum`), `allowed-tools`/`tools`, `command`/`slash`, `version` 与连字符别名; 产品私有键 (`handler`/`workflow`/`params`) 不破坏 agent-studio 加载器 (其 `parse_skill_md` 归入 `extra`)。内置 `/contract-review` 包已改用共享格式。`FUSION_COWORK_SKILLS_DIRS` (冒号分隔) 增加共享安装目录, 单文件一次安装两侧可用。规范: `docs/skill-packages.md`。测试: `tests/test_issue_167_shared_skill_format.py` (21 例)。
- **网关签发模块密钥 (#168, K4)** — shell 注册体不再携带模块自供 `api_key` (除非显式配置 `FUSION_COWORK_WEB_API_KEY`, 静态覆盖路径向后兼容)。网关 (K1) 在注册响应返回签发 `fm-` 密钥时, cowork 一次性持久化到 `~/.fusion-cowork/web_module_key` (0600, 明文永不落日志 — 仅掩码 `fm-x****xxxx`), 心跳改用签发密钥而非 admin key; 心跳 401 回退重注册。入站 `/api` 鉴权在 24h 双活窗口内同时接受 env 静态密钥与签发密钥 — 全新安装零手工配密钥, 存量安装升级不停机。测试: `tests/test_issue_168_gateway_keys.py` (15 例)。

### fusion-browser 托管后端 (v0.5.30, issue #162)

实现 fusion-browser#25 契约的 cowork 侧（`fusion-browser/docs/cowork-managed-backend-contract.md`）：

- **托管启动** — `fusion_cowork/fusion_browser.py` 用 `FUSION_BROWSER_CONFIG` 指向每运行独立配置（socket、随机 auth token、`tokenCapabilities: ["all"]`、显式 `allowedOrigins`）拉起 fusion-browser 引擎，等 socket 通过 `metrics` 鉴权，收尾发 SIGTERM；绝不读写使用者的 `~/.fusion-browser`。
- **每运行 profile** — `create_session(profile_id="run-<id>")` 把登录态放进引擎持久 profile；run→profile 映射写入 `~/.fusion-cowork/fusion-browser/profiles.json`，恢复的运行可重新挂载；`finish` 关闭会话并删除 profile（`keep_profile` 可保留）。
- **登录态导入** — 可选 `import_cookies(session_id, browser, profile)` 返回引擎的诚实计数（`imported`、`skipped_app_bound`、`skipped_other`），UI 原样展示，绝不笼统显示"已登录"。
- **面板截图流** — 第二条直连 UDS 连接跑 `stream_screenshots`；`GET /api/fusion-browser/stream` 以 SSE 转发 `screenshot_frame`，`stream_end` 视为终止。Web `BrowserPanel` 增加 fusion-browser 模式渲染该流。
- **节点接线** — `fusion_browser` 节点（TOOL，高危）在同一托管引擎上暴露 launch/session/import/execute/screenshot/finish；`cdp_*` 节点不变。

上游 Python 客户端以字节一致方式 vendor 到 `fusion_cowork/vendor/fusion_browser_client/`（MIT，无 PyPI 包）。

测试：`tests/test_issue_162_fusion_browser.py`（13 例：独立配置 + 环境变量、启动回滚、会话/profile 持久化、诚实 cookie 计数、execute 映射、收尾清理、截图流、节点与 API/SSE 面，均对假 UDS 引擎）。

### 托管浏览器、Markdown 技能包、跨设备接管、管理台 (v0.5.29, issues #153/#154/#155/#156)

- **托管浏览器 (#153)** — `fusion_cowork/managed_browser.py` 与 `managed_browser` 节点自动拉起独立 `--user-data-dir`（`~/.fusion-cowork/browser-profile`）和自动分配 `--remote-debugging-port` 的 Chromium/Chrome/Edge/Brave，成功后设置 `FUSION_BROWSER_CDP`，现有 `cdp_*` 节点直接复用同一会话，无需手工配置 Chrome 调试端口。可选 `import_cookies` 通过 `Network.setCookies` 导入 Netscape `cookies.txt`（登录态不出本机）。`web serve` 新增 `POST /api/browser/launch|close`、`GET /api/browser/status|screenshot`；Web 侧栏 `BrowserPanel` 每 2 秒刷新运行截图。`FUSION_COWORK_CHROMIUM` 可覆盖可执行文件发现。
- **Markdown 技能包 (#155)** — `fusion_cowork/skills/packages.py` 从内置 `skills/packages/` 与 `~/.fusion-cowork/skills/` 发现 `*.md` 包（YAML frontmatter: `name`/`params`/`handler`/`output_template`/`aliases`；正文即指令）。参数 schema 驱动 Web `#/skills` 表单（`GET /api/skills`、`POST /api/skills/{name}/run`）；handler 可委托内置技能（`builtin:/spreadsheet-recon`）、运行参数化 DAG 或调用本地 Python handler。种子技能: `/contract-review`（合同 + 可选 fusion-kb 要点 + 本机 MLX 备忘，逐条引用）、`/spreadsheet-recon`、`/weekly-readout`（生命周期 JSONL 汇总）。CLI: `skill packages`、`skill run-package`。
- **跨设备接管 + 推送出口 (#154)** — `WS /ws/takeover/{run_id}` 让已认证设备接入运行中的 run（快照帧、事件泵、`directive`/`pause`/`resume`/`cancel`/`confirm` 帧）；`PushEgress` 把 `run_finished` 与审批入队事件推到 `FUSION_COWORK_PUSH_WEBHOOK_URL`（尽力而为，未配置零行为变化）；MobilePage 增加暂停/恢复/取消与允许/拒绝。
- **管理台 + OTel 审计导出 (#156)** — `/api/admin/*` 由 `FUSION_COWORK_ADMIN_KEY` 把关（未配 503，错误 401）：成员/用量/推送测试，`GET /api/admin/audit/export` 输出 OTLP/JSON `LogsData`（审计链 + 终态运行）。新增 `#/admin` 页面，管理密钥仅存浏览器 localStorage。

### GUI 审计修复: 逐块 diff 审阅/token 瀑布/委托即时预览 (v0.5.28, issues #142/#143/#144)

按 fusion-all-gui-audit-1021 §3.3/§3.4/§3.7 验收口径落地:

- **审批 diff 逐块审阅 (#142)** — 编辑型待审批 (`apply_edit` 类 `old_text`/`new_text` 或 `old_string`/`new_string` 参数) 以 SequenceMatcher 分块渲染, 每块可独立**接受/拒绝**; 存在拒绝块时 "仅本次允许" 携带 `accepted_blocks`, 后端经 `apply_block_selection` 重建窄化后的编辑参数 (拒绝块回退原文) 并回显 `edited_params`; 全部拒绝则 confirm 不生效且不落规则。`engine/ast_diff.py` 新增文本 diff 引擎 (`text_diff_blocks` / `apply_block_selection` / `unified_diff_text`) 接入审批链路。文件产物以 unified diff 卡呈现: 编辑对输入与 `diff` 输出产出 `type: "diff"` 产物 (`.diff`/`.patch` 文件同), run 详情产物面板按 +/- 行着色渲染。权限检查现可见生效参数 (`config.params` 与上游输入合并), config 参数节点审批卡不再为空。
- **token 瀑布 (#143)** — `WorkflowStep.tokens` 自节点输出 `usage` 提取 (`_extract_tokens`: prompt/completion/total); AI 节点 (分类/摘要/重命名) 跨批次/跨文件聚合 usage 落输出; `_step_dict` 暴露 `tokens`; RunsPage 瀑布每行显示 `tok`, run 头部显示 `Σ N tok`, 被拒绝节点虚线边框 + 被阻塞 chip + 琥珀色条。
- **委托即时预览 (#144)** — DelegatePage 输入停顿 800ms 自动生成 DAG 预览 (≥6 字符, 过期响应序号守卫); 预览卡就地编辑 (工作流名/描述、节点标签 + 参数 JSON 编辑器、删节点连带清边、边删除/新增), 确认保存才落库。

测试: `tests/test_issue_142_approval_diff.py` (15 例: 分块/选择重建、待审批详情 + 引擎链路、accepted_blocks 窄化/全拒/全收、diff 产物来源) + `tests/test_issue_143_run_tokens.py` (5 例: token 提取、引擎捕获、`_step_dict` 兼容、classify usage 聚合)。

### GUI/UX 升级: 审批卡/瀑布/空间指令 (v0.5.27, issue #138)

按 fusion-all-gui-1020 §6 cowork 目标落地 (G6/G7/G10 + 模式 C2/C3/C6):

- **审批卡 + 规则持久化** — `web serve` 启动加载权限规则 (`PermissionManager.load()`), 每次 confirm 后落盘, 审批规则重启不再丢 (此前纯内存: 每次重启都要 拒绝→批准→重跑)。`confirm_guard()` 支持 `scope` — ApprovalsPage 卡片提供 **三动作**: 拒绝 / 仅本次允许 (从参数快照推导精确 scope, 如 `command:git` 或 `file:/path/**`) / 始终允许 (scope `*`)。本地待审批条目附带卡片上下文 — 脱敏参数快照 (仅标量, 200 字上限, 秘钥键脱敏)、`ts`, 以及引擎在权限检查点经 ContextVar 写入的 `workflow`/`node_id`。`command` 与 `old_string`/`new_string` 参数以 diff 风格块渲染。
- **运行瀑布** — `RunRegistry._step_dict` 暴露 `input_data`; RunsPage 详情按节点渲染瀑布行 (状态/时长条 ∝ 执行时间); 点击行展开抽屉查看完整 input/output/error。
- **空间知识层** — `SpaceConfig.instructions` (存于既有 `spaces.config` JSON 列, 无迁移); `GET/PUT /api/space/spaces/{id}/instructions`; `SpaceChatService` 在流式聊天与两条 agent 路径把 `[空间指令] <文本>` 注入为首条 system 消息; SpacePage 知识页新增指令编辑器。
- **委托打磨** — 预览卡 + 保存跳画布 v0.5.8 (W7) 已交付; 模型 chat 过滤来自 #126/#131。

测试: `tests/test_issue_138_gui_upgrade.py` (10 例: save/load 往返、scoped confirm、待审批卡上下文、引擎上下文 + 瀑布 `input_data`、instructions API 往返 + 聊天注入)。

### Shell Manifest 扩展字段 (v0.5.26, issue #139)

`web/manifest.json` 现声明 fusion-web-shell#24 扩展字段，cowork 由此接入 shell 宿主的 ⌘K 全局搜索、通知中心与侧栏深链：

- **`pages`** — 7 条模块内相对路由（`/workflows` `/delegate` `/canvas` `/runs` `/sessions` `/spaces` `/approvals`），中英标题；宿主以 `${entry}#${route}` 深链。
- **`searchProviders`** — 单一 provider（`cowork`）指向 `/shell/fusion-cowork/api/search`；`GET ?q=` 聚合工作流定义（路由 `/canvas/<id>`）、终态运行（`/runs`）与协作空间（`/spaces`），大小写不敏感子串匹配，空查询返回空，上限 32 条。
- **`notifications`** — 两个轮询源：`approvals`（`/shell/fusion-cowork/api/notifications/approvals`，包装 `list_pending()` — 每条待审批一项，`level: warning`，id 为稳定 `action_id`）与 `runs`（`/shell/fusion-cowork/api/notifications/runs`，终态运行按时间倒序，失败 `level: error`，id 为稳定 `run_id`）。id 跨轮询稳定，满足宿主去重合同（`${moduleId}:${sourceId}:${itemId}`）。

测试: `tests/test_issue_139_manifest_ext.py` (15 例: manifest schema 合规 + 搜索/通知形状、id 稳定性、上限)。

### Runs API 坏边形状 400 修复 (v0.5.25, issue #135 F-2)

#135 的 F-2 是**本仓** union-web API 的问题（在 agent-studio 的镜像 issue 已以误报关闭）：`POST /api/runs` 携带 `{"from": "n1", "to": "n2"}` 形状的边时，`Workflow.from_dict` 内部 `KeyError: 'source_id'` 直接 500。

- **`Edge.from_dict`** — 缺失/空白 `source_id`/`target_id` 现抛可读 `ValueError` 并指名缺失字段（对齐既有含环 `ValueError` 先例）；`Workflow.from_dict` 边解析统一走该入口（顺带消除同逻辑的内联重复）
- **`runs_create`** — 捕获 `Workflow.from_dict` 的 `ValueError`（坏边形状/含环）→ `400` 返回 `工作流定义无效: <原因>`，不再 500

测试: `tests/test_issue_135_runs_edge_400.py` (9 例: Edge/Workflow 校验 + API 400/202)。

### Studio graph execute 422 修复 (v0.5.24, issue #135)

#133 真机跨模块验收发现 `studio_run` 的 `graph_id`+`mode=sync` 路径必现 `agent-studio HTTP 422` — 上游 `GraphExecuteRequest` (`agent_runtime/api_server.py`) 把 `graph_id` 声明为**必填 body 字段**（与路径参数冗余但强制校验）。`_execute_graph` 现发送 `{"graph_id": ..., "input_text": ...}`；wire 契约测试断言该 body 字段（F-2 `/api/runs` 坏边形状 500 属 agent-studio 侧缺陷，已镜像提上游 issue）。

测试: `tests/test_issue_133_studio_node.py` (18 例)。

### Agent-Studio 运行节点 (v0.5.23, issue #133)

跨模块组合：cowork DAG 现在可以把 fusion-agent-studio 的 graph/agent 作为一步调用。`StudioRunNode` (`fusion_cowork/nodes/ecosystem/studio_node.py`, 节点名 `studio_run`, 别名 `运行Studio`/`Studio运行`, ecosystem 分类 3→4 节点 / 总数 68):

- **路径** — `graph_id` + `mode=sync` → `POST /v1/graphs/{id}/execute` (全量 `events` 内联返回); 其余走 `POST /v1/runs` 提交 — `mode=async` 立即返回 `run_id` 交上游编排, `mode=sync` 轮询 `GET /v1/runs/{id}` 至终态 (`completed`/`failed`/`canceled`) 后取 `GET /v1/runs/{id}/result`。sync 超时尽力经 `POST /v1/runs/{id}/cancel` 取消, 带 `run_id` 显式失败。
- **配置** — env `FUSION_AGENT_STUDIO_URL` (默认 `http://127.0.0.1:11442`), 可选 `FUSION_AGENT_STUDIO_API_KEY` 以 `x-api-key` 携带 (上游本地受信模式鉴权可选)。参数: `graph_id`/`agent_id` (二选一), `input`, `mode`, `timeout` (300s), `poll_interval` (2s), `title`, `idempotency_key`。输出: `run_id`, `status`, `result`。
- **门控** — `studio_run` 加入 `HIGH_RISK_NODES` (触发远端多步 agent 执行, 与 `fetch_url` 同级须显式放行); guard 内容映射走 JSON fallback。

测试: `tests/test_issue_133_studio_node.py` (18 例, httpx MockTransport 离线桩)。

### 模型 chat 过滤加固 (v0.5.22, issue #131)

跨仓对齐 fusion-gateway#245 (网关在 `/v1/models` 上新增 `engine_type` + `modality` + `capabilities` 元数据)。`_is_chat_model` (`fusion_cowork/ai/nl_parser.py`, 由 `/api/models` 与 `_pick_chat_model` 共用) 闭合两个缺口:

- **`draft` 引擎类型过滤** — 投机解码草稿模型 (如 `Qwen3.8-27B-DFlash2`) 加入 `_NON_CHAT_ENGINE_TYPES`, 不再可能被选为 chat 默认模型。
- **`capabilities` 第二信号** — 模型条目带网关 `capabilities` 元数据时 (顶层或 `metadata.capabilities`), 优先于 `engine_type` 判定: `capabilities.embedding=true` → 非 chat (无 `engine_type` 也正确, 如 bge-m3 `modality=text`), `text_generation=false` → 非 chat, `text_generation=true` → 权威 chat。不含 `embedding`/`text_generation` 键的条目照旧走 `engine_type` 门。

测试: `tests/test_issue_131_chat_model_filter.py`。

### Shell 嵌入模式 API 前缀 (v0.5.21, issue #129)

统一 shell 下模块被托管在 `/shell/<id>/` — 根相对 `/api/...` 路径全部 404 (网关无通用 `/api/*` 路由)。API 层现从 document pathname 推导模块前缀 (对齐 agent-studio 的 `apiBase()`):

- **`web/src/sdk.ts::apiBase()`** — `/shell/fusion-cowork/runs` → `/shell/fusion-cowork`, 独立根路径 → `""`。应用于独立 `directRequest`、`streamSse` 与嵌入 postMessage bridge 路径 (host 按原样 fetch, 前缀必须随路径携带)。一份构建同时服务两种拓扑。
- **契约测试** — `web/tests/sdk.test.ts` 新增 `apiBase` describe (前缀推导、带前缀请求、独立根路径无前缀)。

---

## 🕸️ 统一 Web 模块 (v0.5.5, issue #94; v0.5.6 加固, issue #96/#97/#98)

### 独立模式鉴权与网关注册 (v0.5.6, issue #96/#97) + P0 页面 (v0.5.7) + P1 委托与会话 (v0.5.8)

- **运行时密钥接缝**: 独立发布构建下 API key 运行时解析 — `window.__FUSION_COWORK_WEB_API_KEY__` > `localStorage['fusion-cowork-web-api-key']` > 构建期 `VITE_API_KEY` (遗留回退)。无 key 则省略鉴权头 (keyless serve 姿态, 127.0.0.1 绑定)。独立 HTTP 包装器对带 body 请求始终补 `content-type: application/json` (上游 fusion-web-shell#22)。
- **网关 shell 注册**: `web serve` 启动后向 fusion-gateway 注册 `web/manifest.json` 并心跳保活 — 需同时配置 `FUSION_SHELL_GATEWAY_URL` + `FUSION_ADMIN_KEY` (经 `fusion_core.config.admin_key()` 单点解析, issue #204; `FUSION_SHELL_MANIFEST` 覆盖 manifest 路径)。未配置则完全跳过。对齐 agent-studio 的 `shell_registry`。

浏览器里的 DAG 工作流画布 — fusion-cowork 接入统一 Web Shell (PRD N-6)。

- **`web/` UI 包** — React 18 + Vite + TS + Tailwind (preset 来自 `@fusion/web-ui`) + Bun; 画布用 `@xyflow/react`。工作流列表 (运行/编辑/删除, 删除带名称二次确认)、桌面端 DAG 画布编辑 (节点面板来自 `/api/nodes`、连线、节点参数编辑含 schema 提示)、运行视图实时轮询 (可见 5s / 后台 Tab 降频 30s)、移动端只读 (`devices: ["desktop", "mobile-readonly"]`)。内嵌模式 (`?embedded=1`) 隐藏自有导航 — Shell 管外壳。
- **`fusion-cowork web serve`** — Web API :11461: 节点目录、工作流定义 CRUD (原子 JSON 存储)、运行生命周期 (异步提交 → 轮询 → 停止, 共享 `WorkflowEngine`)、模板目录 (`/api/templates`, 内置模板含 workflow 体快照)、定时任务 (`/api/scheduler/tasks` CRUD + 暂停/恢复, 底层引擎 `TaskScheduler`; `web serve` 启动调度器, cron/间隔任务随 uvicorn 事件循环触发)。同服务静态托管 `web/dist`, 供 gateway `/shell/fusion-cowork/` 挂载。鉴权: `FUSION_COWORK_WEB_API_KEY` (gateway 代理注入; 未配置放行, 默认仅 127.0.0.1 绑定)。
- **P0 页面 (v0.5.7, 迁移方案 §6)**: 节点浏览 (W2, 搜索 + 分类色标 + 参数 schema)、模板目录 (W5, 一键「用画布打开」把模板体复制为用户工作流)、调度管理 (W6, 创建 cron/间隔任务、暂停/恢复/删除)、运行状态过滤 (W4)、画布 `?view=1` 分享链接只读 (W3)。
- **P1 委托与会话 (v0.5.8)**: NL 委托入口 (W7 — 自然语言 prompt → `/api/ai/generate-workflow` 经 `NLWorkflowGenerator` → 节点草稿预览 → 保存到画布 / 保存并运行)、会话统一列表 (W11 — `/api/sessions` 走引擎 `SessionStore`)。`create_web_app` 新增 `nl_generator=` / `session_store=` 注入接缝。W8 运行改向 + W9 交付审查需新交互协议 (独立批次); W10 artifacts 挂 space 域, 随 P2 空间面落地。
- **P2 空间面 (v0.5.9)**: 空间页 (W12 — 侧栏列表/创建/删除 + 详情面板)、空间聊天 5s 轮询 (W13, 以 `local_user` 发送; SSE 升级后续做)、成员标签页 (来自 space 成员角色)、artifacts 标签页 (W10 — 列表/创建/预览, 走挂载子应用新增 `/api/space/spaces/{id}/artifacts` 路由)。`create_space_api` 新增 `artifact_svc=` 参数; `create_web_app` 新增 `space_api=` / `space_data_dir=` 接缝 (默认 `_build_space_stack` 写 `~/.fusion-cowork/spaces`; 挂载在 `/api/space`, 同一静态密钥鉴权之下)。`web serve` 关停时关闭 `SpaceStore`。
- **P2.5 空间收口 (v0.5.10)**: KB 面板 (W14 — 状态 + 绑定/解绑 + 检索搜索 + 文档列表, 走已有 `/api/space/.../kb/*` 路由; 实时检索需 fusion-kb :11436 在线)、通知中心 (W15 — 顶栏铃铛未读数 30s 轮询 + 下拉列表逐条已读, 走挂载子应用新增 `GET /notifications` + `POST /notifications/{id}/read`; `create_space_api` 新增 `notification_svc=` 参数, `_build_space_stack` 接 `NotificationService`)、成员权限矩阵 (W16 — 成员行内角色 `<select>` + 移除按钮, 走新增 `PUT /spaces/{id}/members/{user_id}` (鉴权 + `manage_members` 守卫) 与只读 15 动作 × 4 角色矩阵 `GET /spaces/{id}/permissions`; 既有 `DELETE .../members/{user_id}` 加固为 operator 取可信 principal, 不再读 query 参数, A-5/CR-5)。
- **P3-1 运维面 (v0.5.11)**: 模型目录 (W17 — `GET /api/models` 走 `FusionMLXClient.list_models()`, fusion-mlx 离线返 503; 委托页新增模型下拉, 选择持久化 `localStorage`, 以可选 `model` 覆盖传入 `/api/ai/generate-workflow` — 后端按请求构造临时生成器, 不改共享实例状态)、插件面板 (W20 — 新页面走既有 `rpc_bridge` 委托: web API 新增 `POST /rpc` JSON-RPC 2.0 端点路由 `plugins/*` (非 plugins 方法 -32601, 缺 `fusion-plugins-ecosystem` -32603, JSON 解析失败 400/-32700); 插件列表 + 安装/卸载操作)。W19 guard 审批 / W18 配额 / W21 集群 / W22 移动投递待后续。
- **P3-2 审批 + 集群 (v0.5.12)**: guard 审批队列 (W19 — `PermissionManager.list_pending_guard()` 快照 + `GET /api/permissions/pending` + `POST /api/permissions/pending/{action_id}/confirm` (仅 fusion-guard 启用时 confirm 落地; `create_web_app` 新增 `permission_manager=` 接缝并注入默认引擎)), 集群面板 (W21 — 只读 `GET /api/cluster/state` 走分布式状态层: 节点注册表 + 心跳状态 + 按插件 vRAM 账本; 未配 `FUSION_CLUSTER_ENABLED` 返 `cluster_enabled: false`)。W8/W9 (交互协议)、W18 配额、W22 移动投递仍待后续。
- **P4 交付审查 + 用量 (v0.5.13)**: 产物审查状态机 (W9 — `SpaceArtifactService.review_artifact()` owner/admin 通过/退回, 状态落 `metadata.review` (无 schema 变更), `POST /api/space/spaces/{id}/artifacts/{aid}/review`; 产物页签加 通过/退回 按钮 + 状态徽标), 用量/配额指示 (W18 — `GET /api/usage` 聚合租户计数 (spaces/messages/artifacts/agents) + QuotaEnforcer 配额, UsagePage 表格)。剩余: W8 运行改向、W22 移动投递。
- **W22 移动只读 + 投递 (v0.5.14)**: `GET /api/mobile/overview` (近期运行 + 待审批 guard 快照), `POST /api/deliver` (运行投递到桌面, 走跨设备同步引擎; `create_web_app` 新增 `cross_sync=` 接缝 — 未注入 503, 离线/未知设备诚实返 `delivered: false`, 否则广播语义), `#/m` MobilePage (运行卡片 + 投递按钮 + 待审批, 只读)。剩余: W8 运行改向 (orchestrator 交互协议)。
- **W8 运行改向 (v0.5.15)**: 引擎级暂停/恢复 — `WorkflowEngine.pause/resume/is_paused` 在下一节点检查点挂起执行 (按执行粒度 asyncio.Event; 当前节点完成后等待, cancel 唤醒暂停中的执行); `RunRegistry.pause/resume` 记录 `status: paused`, `POST /api/runs/{id}/pause|resume` 非运行态/非暂停态返 409; RunsPage 加 暂停/恢复 按钮 (`paused` 徽标 + 状态筛选项)。追加指令改向需 orchestrator 交互协议 (I2) — 不在本批。迁移文档 §6 完成 (W1–W22)。
- **共享 harness 内核 (v0.5.16)**: 迁移 #109 — `engine/hooks.py` 与 `engine/permission.py` 改为 fusion-harness 垫片 (对标 fusion-agent-studio #357): 安装 harness 时再导出内核类型 (`HookEngine`/`HookConfig`/`HookResult`, `PermissionEngine`/`PermissionRule`/`PermissionDecision`/`PermissionMode`), 由 `KERNEL_SOURCE` 标注当前来源; 树内 `_hooks_local.py`/`_permission_local.py` 保留 cowork 专有语义 (14 事件 `HookEvent`、优先级排序、`PRE_*` fail-closed、`HookContext.modified_data`、5 级 `PermissionLevel`、`HIGH_RISK_NODES` guard 委托、`confirm_guard`、原子 save/load)。安装: `pip install -e ../fusion-harness` + `.[harness]`; 未装 harness 自动回退本地副本 (CI 安全)。
- **funnels + compactor 接入 (v0.5.17)**: issue #111 — `engine/compactor.py` 与 `engine/funnels.py` 改为 fusion-harness 垫片 (安装 harness 时再导出内核类型, `KERNEL_SOURCE` 标注来源, 树内逐字回退副本保证 CI 绿)。真实消费: `SpaceChatService` 所有流式/Agent LLM 调用经 `LLMFunnel` (生命周期 `llm_before`/`compact_before`/`compact_after`/`llm_after` + `Compactor` 9000 token 窗口对齐 24000 字符预算, `_trim_context` 作为最终兜底), `EventEmitter.attach_lifecycle_bus()` 把工作流事件镜像到进程级 `LifecycleBus` (内存环形缓冲; `FUSION_COWORK_LIFECYCLE_DIR` 落 JSONL 供回放)。`engine/lifecycle.py::get_lifecycle_bus()` 为共享单例, CLI runtime 与空间对话均已接线。
- **运行交互协议 + 实时面 (v0.5.18, issues #113–#118)**: 节点边界运行改向 (`WorkflowEngine.add_directive` — `set_params` 默认目标为下一节点, `insert_node` 支持 `after_node_id` + 注入输入, `skip_remaining` 把剩余节点标记 `SKIPPED`; `POST /api/runs/{id}/directive` 入队, `TrajectoryRecorder.record_directive` 审计); 运行级交付审查 (成功完成置 `review: pending_review`, `POST /api/runs/{id}/review` 通过/退回, 退回返回工作流定义供迭代; 与 W19 guard 节点审批区分); 运行产物预览 (`GET /api/runs/{id}/artifacts` 从步骤输出提取文本产物 — `file_path`/`output_path`/`path`, 64 KB 上限, 最多 20 项, inline markdown/html/text 兜底); 统一会话列表 (`GET /api/sessions?source=engine|space|scheduler` 合并三源, 带 `source`/`title`/`space_id`/`space_name`/`href`, 按 `updated_at` 倒序); 实时通道 (`GET /api/runs/{id}/events` SSE 快照 + 引擎 `EventEmitter` 广播, 空间 `/stream` 改用与聊天共享的 emitter 并先发 `ready` 帧; 前端因 `EventSource` 无法带运行时 API key 改用 fetch 式 SSE, 轮询保留为降级与内嵌模式回退); 顶栏全局模型选择 (`web/src/modelPref.ts`, 持久化 `localStorage['fc-preferred-model']`, 与委托页内联选择共享, 透传 `/api/ai/generate-workflow` 与空间 agent 聊天新增的 `model` 字段)。
- **Union-web 合规**: manifest 符合 PRD §5.1, vendor `@fusion/web-sdk`/`@fusion/web-ui` (与 shell 仓逐字节一致), 四态视图 (骨架/空/错误/重试), 全 token 色, 无原生 alert/confirm, UI 不含鉴权逻辑。
- **开发端口已登记**: 11461 (web API), 11464 (vite dev) — `architecture/port-registry.yaml`。

```bash
# 后端
fusion-cowork web serve                     # :11461 (REST + 静态托管)

# 前端 (开发)
cd web && bun install && bun run dev        # :11464, 代理 /api → :11461
bun run build                               # dist/ → web serve / gateway 托管
```

---

## 🧠 Run 级记忆集成 (v0.5.29, issue #157 / fusion-memory#20)

Run 现在自动与 **fusion-memory**（生态长期记忆中枢）闭环 — 无需在 DAG 里手工摆放 `memory_commit`/`memory_retrieve` 节点。任务产出以短期记忆进入 fusion-memory，按复用晋升长期（三线调度 + 遗忘曲线在引擎侧，已上线）。

- **Run 启动（召回）** — 记忆开启时，run 以 `<工作流名> <描述>` 查询 fusion-memory，召回上下文经 `initial_input` 以 `memory_context` 键注入首节点输入；run 记录以 `memory_context` 字段暴露。
- **Run 结束（写回，仅 success）** — 执行步骤构建为 fm-core `Interaction`（单 turn：任务 = user_message，结果摘要 = assistant_message，步骤 = tool_calls，封顶 64 步 / 每字段 4000 字符），经 fm-server `commit` 写入；run 记录暴露 `memory_writeback`（`ok`/`failed`）+ `memory_ids`。
- **Per-run 开关** — `POST /api/runs` body `{"memory": false}`（默认 `true`），`desk.workflow.run` params `memory`，Web 画布运行栏 + 委托页有「记忆」勾选框。关 = 零 RPC 调用、零痕迹。
- **零冲击降级** — `FUSION_MEMORY_API_KEY` 未配 → 集成整体关闭（与旧版行为一致）；fusion-memory 不可达 → run 照常成功，失败以 `memory_writeback: "failed"` + warn 日志 fail-visible。离线硬约束保持：仅 HTTP 到 127.0.0.1。

实现：`fusion_cowork/memory_bridge.py`（wire 契约对齐 `fm-core` interaction.rs + fm-server `commit` P1-1 结果形状）。测试：`tests/test_memory_bridge.py`、`tests/test_run_memory.py`（离线 MockTransport）+ `tests/test_memory_e2e.py`（门控 e2e，需真 fm-server）。

启用：

```bash
# fm-server (fusion-memory) 需在 11435 运行且开启 HTTP 鉴权
export FUSION_MEMORY_API_KEY=<fm-server api key>
export FUSION_MEMORY_BASE_URL=http://127.0.0.1:11435   # 可选, 默认值
```

Run 级关闭（API）：

```bash
curl -X POST http://127.0.0.1:11463/api/runs -H 'Authorization: Bearer <key>' \
  -d '{"workflow_id": "wf1", "memory": false}'
```

门控 e2e（需真 fm-server + fusion-mlx）：

```bash
FUSION_MEMORY_E2E=1 FUSION_MEMORY_API_KEY=<key> pytest tests/test_memory_e2e.py -v
```

---

## 📬 团队消息 — durable 信箱 + 常驻消费池 (v0.5.39, issue #190)

跨进程、跨项目的 agent 消息面，不需要任何外部 RPC 拉动。cowork 常驻一个**消费池**：花名册里登记的每个 agent 自己消费信箱，把消息派发给工作流，跑完把结果作为回信投回发送方。

- **Durable 信箱** — 消息落 SQLite (`~/.fusion-cowork/team.db`)，不是内存队列。未 ack 消息重启不丢；派发拿到 run id **之后**才 ack，崩溃后重投（at-least-once）。
- **与 agent-studio 同构** — `mailbox` 表与 `agent_runtime/durable_roster.py` 完全一致。两个 daemon 指向同一个文件（`FUSION_TEAM_DB` / `FUSION_COWORK_TEAM_DB`）即可让 cowork agent 与 agent-studio agent 直接对话。cowork 自己的花名册落在独立 `cowork_roster` 表，两个 daemon 不会互抢消息。
- **推送 + 轮询** — 本地发送经 `TeamMailbox.on_send` 立即唤醒消费池；远端 daemon 可 `POST /api/team/notify`；`FUSION_INBOX_POLL_INTERVAL`（默认 5s）兜底没有通知能力的对端。
- **无需人工拉动** — 推进对话不需要任何 `desk.*` RPC 调用。花名册有启用 agent 时 `web serve` 自动拉起消费池（`FUSION_COWORK_RUNNER_POOL=1` 强制常驻）。
- **可观测** — `GET /api/team/status` 返回活 consumer 数、每 agent 积压、最老未 ack 年龄、已消费/回信/错误计数；同一组指标导出 OTLP：`fusion_team_mailbox_depth`、`fusion_team_oldest_unacked_age_seconds`、`fusion_team_mailbox_consumed_total`、`fusion_team_mailbox_replies_total`。

```bash
# 登记常驻 consumer，绑定工作流
fusion-cowork team register reviewer --workflow wf-review --team qa

# 发消息（本地发送即刻唤醒消费池）
fusion-cowork team send --from alice --to reviewer "review PR 42"

# 查看
fusion-cowork team roster
fusion-cowork team inbox reviewer
fusion-cowork team status        # 需要 web daemon 在 :11461

# 消费池生命周期
fusion-cowork team start
fusion-cowork team stop
```

与 fusion-agent-studio 共享信箱：

```bash
export FUSION_TEAM_DB=~/.fusion-team/team.db   # 两个 daemon 读同一文件
```

完整链路、线上契约与失败语义见 `docs/team-messaging.md`；测试 `tests/test_issue_190_mailbox_consumers.py`。

---

## 🔧 节点参考

### macOS 系统节点

| 节点 | 功能 |
|------|------|
| `desktop_clean` | 桌面按类型/日期规整 |
| `download_organizer` | 下载文件夹归档去重 |
| `disk_cleaner` | 磁盘缓存垃圾清理 |
| `file_watcher` | 目录变化监听 |
| `screen_capture` 🆕 | 桌面截图（全屏/选区/窗口） |
| `clipboard` 🆕 | 系统剪贴板读写 |
| `notification` 🆕 | macOS 通知中心推送 |
| `app_lifecycle` 🆕 | macOS 应用启动/退出/激活/列表 |
| `ocr` 🆕 | 图片文字识别（Vision + MLX） |

### AI 处理节点（→ 调用 fusion-mlx）

| 节点 | 功能 | 后端 |
|------|------|------|
| `ai_classify` | 语义文件分类 | `fusion-mlx /v1/chat/completions` |
| `ai_summarize` | 文档摘要生成 | `fusion-mlx /v1/chat/completions` |
| `ai_generate_name` | 智能文件命名 | `fusion-mlx /v1/chat/completions` |

### 工具节点（吸纳自 Squish 内置工具集）

| 节点 | 功能 |
|------|------|
| `shell_exec` | 执行 Shell 命令，捕获输出 |
| `python_repl` | 在隔离子进程执行 Python 代码 |
| `web_search` | 通过 DuckDuckGo Lite 搜索网页 |
| `fetch_url` | 获取 URL 内容，自动提取文本 |
| `apply_edit` | 对文件做查找替换编辑 |

### IO / 逻辑节点

| 节点 | 功能 |
|------|------|
| `file_input` | 读取文件列表或目录内容 |
| `file_output` | 将工作流结果写入文件 |
| `xlsx_write` | 从 JSON spec 生成 Excel 工作簿：多 sheet、公式（打开时重算）、表头样式、条件格式（issue #151；需 `pip install -e ".[office]"`） |
| `pptx_write` | 从 JSON spec 生成可编辑 PPT：标题/内容/图表/表格版式，`period_compare` 自动打标 >10% 变动（issue #152；需 `pip install -e ".[office]"`） |
| `filter` | 按扩展名/大小/日期过滤 |
| `loop` | 批量处理列表中的每个元素 |
| `merge` | 合并多个上游数据源 |

### Chrome CDP 节点 🆕 (M3)

| 节点 | 功能 |
|------|------|
| `cdp_navigate` | 通过 CDP 导航到 URL |
| `cdp_snapshot` | 获取页面无障碍树 |
| `cdp_click` | 按 backendNodeId 点击元素 |
| `cdp_fill` | 按 CSS 选择器填写表单 |
| `cdp_fill_form` | 批量填写多个表单字段 |
| `cdp_screenshot` | 截取页面截图 (PNG) |
| `cdp_evaluate` | 在页面中执行 JavaScript |
| `cdp_emulate` | 模拟设备视口 |
| `cdp_network` | 查询网络请求 |
| `cdp_console` | 查询控制台消息 |

### 插件系统 🆕 (M3)

插件系统支持用自定义节点扩展 Fusion-Cowork：

```python
# 插件清单 (manifest.json)
{
    "name": "my_plugin",
    "version": "1.0.0",
    "description": "自定义节点",
    "nodes": ["my_custom_node"],
    "entry_point": "plugin",
}
```

```bash
# 从目录或 zip 安装
fusion-cowork plugin install /path/to/plugin_dir
fusion-cowork plugin install /path/to/plugin.zip
```

### 技能机制 🆕 (M3)

技能是映射到现有节点的高级快捷方式：

| 技能 | 别名 | 节点 |
|------|------|------|
| `/cleanup` | 清理桌面 | `desktop_clean` |
| `/classify` | AI分类 | `ai_classify` |
| `/screenshot` | 截图 | `screen_capture` |
| `/search` | 搜索 | `web_search` |
| `/organize` | 下载整理 | `download_organizer` |
| `/diskclean` | 磁盘清理 | `disk_cleaner` |

MCP 工具 `skill_list` 和 `skill_run` 可供 Claude Desktop/Code 集成调用。

### Computer Use 节点 🆕 (M4)

鼠标/键盘控制 + AI 循环，实现自主桌面操作：

| 节点 | 功能 |
|------|------|
| `mouse_move` | 移动鼠标到 (x, y) 坐标 |
| `mouse_click` | 点击（左/右/双击） |
| `keyboard_type` | 键盘输入文本 |
| `keyboard_shortcut` | 按组合键（Cmd+C 等） |
| `computer_use_loop` | 截图 → AI 分析 → 操作 → 重复循环 |

```bash
# 移动鼠标
fusion-cowork computer-use move 500 300

# 点击
fusion-cowork computer-use click --x 500 --y 300 --button left

# 输入文本
fusion-cowork computer-use type "Hello World"

# 键盘快捷键
fusion-cowork computer-use shortcut c --modifiers cmd

# AI 驱动 Computer Use 循环
fusion-cowork computer-use run "打开 Safari 并搜索天气" --max-steps 10
```

### 远程控制 🆕 (M4)

基于 WebSocket 的远程控制，支持外部客户端：

```bash
# 启动远程服务
fusion-cowork remote serve --port 11439 --token mytoken

# 从另一台机器连接
fusion-cowork remote connect ws://host:11439/control --token mytoken

# 远程提交工作流
fusion-cowork remote submit workflow.json --url ws://host:11439/control
```

### 结构化输出 🆕 (M4)

JSON Schema 验证节点输出：

```bash
# 验证数据是否符合 schema
fusion-cowork schema validate data.json schema.json

# 检查节点输出 schema
fusion-cowork schema check mouse_move
```

NodeResult 支持 `schema` 字段和 `validate()` 方法，自动进行输出验证。

---

## 📝 工作流示例

### 桌面每日规整 (JSON)

```json
{
  "name": "桌面每日规整",
  "nodes": [
    {
      "id": "n1",
      "name": "file_input",
      "config": { "params": { "path": "~/Desktop" } }
    },
    {
      "id": "n2",
      "name": "desktop_clean",
      "config": { "params": { "organize_by_type": true } }
    }
  ],
  "edges": [
    { "source_id": "n1", "target_id": "n2" }
  ]
}
```

### AI 一句话生成

```bash
fusion-cowork ai generate "每天晚上9点自动清理下载文件夹并备份到文稿"
```

---

## 🧪 运行测试

```bash
# 安装测试依赖（与 CI 同 recipe）
pip install -e ".[test,cloud,web,office]"
# fusion-harness 内核 — engine shim 必需（issue #198，0.2.1 起已上 PyPI）
pip install fusion-harness
# （monorepo 内可改用：pip install -e ".[plugins]"）

# 运行所有测试
pytest tests/ -v

# 生成覆盖率报告
pytest tests/ --cov=fusion_cowork --cov-report=html
```

---

## 🛣️ 迭代路线

### V0.1 (MVP) ✅
- [x] 基础文件自动化（桌面/下载/文档整理）
- [x] AI 批量处理（分类/摘要/重命名）
- [x] 模板中心（10 个内置模板）
- [x] 自然语言生成工作流
- [x] 生态基础打通（fusion-mlx / Fusion-KB）
- [x] 参数类型强制转换（吸纳自 Squish）
- [x] Lazy Import 架构（吸纳自 Squish）
- [x] 工具节点：Shell、Python REPL、Web 搜索、获取网页、文件编辑

### V0.2 ✅
- [x] 增强调度器（Cron UI、日历视图、任务依赖、统计）
- [x] AI 工作流优化器（瓶颈检测、自动修复、fusion-mlx 建议）
- [x] 批量报表生成（Markdown/HTML、多工作流汇总）
- [x] macOS 原生 .app 打包脚本
- [x] 屏幕截图（桌面截图 + AI 分析）
- [x] 剪贴板集成（读写清空）
- [x] 系统通知（macOS 通知中心）
- [x] macOS 应用生命周期（启动/退出/激活/列表）
- [x] OCR 文字识别（Vision + fusion-mlx）
- [x] MCP 服务模式（16 个工具供 Claude Desktop/Code 调用）

### V0.3 ✅
- [x] 行业自动化模板（设计/开发/数据/运维 — 10 个模板）
- [x] 多智能体联动（注册、任务分解、并行执行）
- [x] 跨设备协同（WebSocket 同步、设备发现、工作流共享）
- [x] 权限模型 (MANUAL/AUTO/PLAN/BYPASS — 4级权限，高风险节点审批)
- [x] Hook 系统 (14 种事件类型 — 节点执行前后、工作流生命周期、权限拦截、会话起止、压缩前)
- [x] 会话持久化 (SQLite — 保存/恢复/分叉工作流，自动清理)
- [x] 流式事件 (EventEmitter — 发布订阅、SSE 推送、缓冲回放)
- [x] MCP stdio 传输 (stdin/stdout JSON-RPC 2.0，供 Claude Code 集成)
- [x] Desk RPC IPC (Unix Domain Socket JSON-RPC 2.0，供 Fusion-Studio 集成)
- [x] Agent 真实执行器 (Node/Workflow/MLX/Shell — 替换模拟执行)

### V0.4 ✅
- [x] 对比基准模块 (CapabilityMatrix — 32项能力、对等率、分类对比)
- [x] BenchmarkRunner (节点/工作流计时、warmup+repeats、汇总统计)
- [x] ReportRenderer (Markdown/HTML/JSON 对比报告 — Cowork vs Desk)
- [x] CLI benchmark 命令 (`fusion-cowork benchmark report/run`)
- [x] 端到端集成测试 (MCP全链路、DeskRPC全链路、Workflow+Permission+Hook+Session+Event)
- [x] DeskRPC 事件/会话/权限处理器 (9个新方法)
- [x] CLI 权限命令 (`fusion-cowork permission level/approve/deny/list`)

### V0.5 ✅
- [x] Computer Use 节点 (鼠标移动/点击、键盘输入/快捷键、Computer Use 循环)
- [x] pyobjc + AppleScript 双后端鼠标/键盘控制
- [x] AI 驱动 Computer Use 循环 (截图 → 分析 → 操作 → 重复)
- [x] 远程控制服务器/客户端 (WebSocket、认证令牌、工作流提交)
- [x] 结构化输出 (OutputSchema + NodeResult.validate)
- [x] CLI 命令: computer-use move/click/type/shortcut/run, remote serve/connect/submit, schema validate/check

### V0.6 ✅ (M5 — 执行强骨)
- [x] AgentRuntime — 每个智能体独立执行环境，消息循环 + 收件箱队列
- [x] CoordinatorExecutor — 通过编排器分派子任务到执行智能体
- [x] AgentMessageBus 集成 — 发布/订阅智能体间通信
- [x] Hook 生命周期集成 — HookManager 优先级系统，新事件 (SESSION_START, SESSION_END, PRE_COMPACT)
- [x] Hook 驱动权限 — 通过 PERMISSION_REQUEST hook 上下文自动批准/拒绝
- [x] PermissionManager 异步检查 — 支持规则匹配前的 hook 拦截
- [x] FusionCoworkSDK — 异步 HTTP 客户端 + 本地回退，编程式访问
- [x] HeadlessRunner — 无 CLI/GUI 工作流执行引擎
- [x] SDK 懒加载导入 — FusionCoworkSDK、HeadlessRunner 通过 fusion_cowork 包访问

### V0.7 ✅ (M6 — 协作空间)
- [x] Space 模型与枚举 — Space, SpaceMember, SpaceMessage, SpaceSnapshot, SpaceConfig, SpaceRole/SpaceStatus
- [x] SpaceStore — aiosqlite + SQLite WAL，9 张表 (spaces, members, messages, comments, agents, snapshots, invites, workflows, artifacts, sync_events)
- [x] SpaceService — CRUD + 归档/解档 + get_or_create + 权限门控操作
- [x] SpacePermission — 4 级角色矩阵 (Owner/Admin/Member/Viewer) + 权限校验 + 角色查询
- [x] SpaceMemberService — invite/join/leave/update_role/remove，邀请码支持过期与次数限制
- [x] CLI space 命令 — `fusion-cowork space create/list/get/archive`，`fusion-cowork space member list/invite/join/remove`
- [x] DeskRPC space 处理器 — 11 个 JSON-RPC 方法 (desk.space.create/list/get/update/archive/delete, desk.space.member.invite/join/list/remove/update_role)
- [x] 58 项测试覆盖 models/store/service/permission/member_service

### V0.7.1 ✅ (M7 — 共享对话 + 知识库绑定)
- [x] SpaceChatService — 共享上下文 + Agent 回复 + 流式推理 (SSE)
- [x] SpaceKBService — fusion-kb 绑定 + 文档管理 + RAG 搜索/查询
- [x] SpaceAPI — 25 个 REST 端点 + SSE 事件流 (`/spaces/{id}/stream`)
- [x] FusionMLXClient 增强 — 端口修正 (11432 经 gateway) + 瞬态错误重试 + 流式健壮性
- [x] KBClient 完善 — create_kb/delete_kb/upload_file/list_documents
- [x] SharedContext — 工作流节点访问空间消息 + KB 搜索/查询
- [x] CLI 扩展 — `space chat` 交互式对话 + `space knowledge bind/status/upload/search/unbind`
- [x] DeskRPC 处理器 — 9 个新方法 (desk.space.chat.send/list/context, desk.space.knowledge.bind/status/upload/search/query/unbind)
- [x] 52 项 M7 测试 (总计 461)

### V0.8.0 ✅ (M8 — Agent 编排 + 多 Agent 接力)
- [x] SpaceAgentRuntime — 空间级 Agent 运行时，支持隔离 (add/remove/list/get/call/chain/register)
- [x] AgentStudioClient — Agent Studio API HTTP 客户端，支持重试 + 导入到空间
- [x] Chat 接力 — SpaceChatService.relay_agents() 顺序多 Agent 链式执行
- [x] SpacePermission — `call_agent` 权限 (owner/admin/member: ✅, viewer: ❌)
- [x] SpaceAPI — 6 个新 Agent 端点 (GET/POST /agents, GET/DELETE /agents/{id}, POST /agents/call, POST /agents/relay)
- [x] Orchestrator 桥接 — register_to_orchestrator() 将空间 Agent 映射为 Agent dataclass
- [x] CLI — `space agent list/add/remove/call/relay` 命令
- [x] DeskRPC — 5 个新 JSON-RPC 方法 (desk.space.agent.list/add/remove/call/relay)
- [x] 35 项 M8 测试 (总计 490)
- [x] KBClient 端口修正 (11432→11436, issue #6)
- [x] desk.project.syncKnowledge — 接收外部知识库文件同步 (issue #7)
- [x] desk.project.importSnapshot — 接收会话快照导入 (issue #8)
- [x] desk.project.exportToProject — 导出空间内容到 fusion-projects (issue #9)

### V0.8.1 ✅ (Issue #3/#4 — Artifact 权限 + FSB 集成)
- [x] SpaceArtifactService — Artifact CRUD + 所有权追踪 + 权限校验 (issue #3)
- [x] 权限矩阵 — 15 个动作 (新增: view/edit/share/transfer_artifact)
- [x] DeskRPC — 7 个 desk.space.artifact.* 处理器
- [x] ModuleRegistry — 侧边栏模块注册 (register/list/enable/disable) (issue #4)
- [x] NotificationService — 审批任务通知推送 (SSE + desk.notification.push) (issue #4)
- [x] DeskRPC — 4 个 desk.module.* + 3 个 desk.notification.* 处理器
- [x] Store 迁移 — space_artifacts 扩展列 + 2 张新表 (sidebar_modules, space_notifications)
- [x] 29 项新测试 (18 artifact + 11 FSB, 总计 519)

### 补丁版本

#### V0.2.11 (补丁) — /rpc 端点托管 + plugins/* 委托 (#48)
- [x] 新增 `server/rpc_bridge.py` — 托管 `/rpc` JSON-RPC 端点, 委托 15 个 `plugins/*` 方法给 fusion-plugins-ecosystem `MCPHandler`
- [x] `mcp_http.py` 新增 `/rpc` 路由 (POST), 仅路由 `plugins/*`, 其余返回 -32601
- [x] `DeskRuntime` 注入 `NodeRegistry` + `TaskScheduler` + `FusionMLXClient`, Studio 可发现 cowork 节点
- [x] 依赖缺失降级: `fusion-plugins-ecosystem` 未安装时 `/rpc` 返回 -32603 + 安装提示 (新增 `[plugins]` extra)
- [x] Bug fix: `desk_rpc._handle_health` 硬编码 `version: "0.3.0"` → 动态读取 `__version__` (版本漂移修复)
- [x] Bug fix: `mcp_http.py` 移除 `from __future__ import annotations` — FastAPI 0.139 下 `Request` 注解字符串化导致路由 422
- [x] Bug fix: `mcp_http.py` `MCPToolRegistry` 改运行时导入 — Py3.11 下 `TYPE_CHECKING` 注解立即求值触发 `NameError` (Py3.14 注解延迟默认故本地未现, CI 3.11 暴露)
- [x] 降级语义: `/rpc` 非 plugins 方法 → -32601 (与依赖无关); plugins 方法依赖缺失 → -32603
- [x] 测试: 14 个 `/rpc` + `rpc_bridge` 用例, `skipif` 降级保护 CI; Py3.11 588 passed/5 skipped, Py3.14 593 passed, ruff 0 issues

#### V0.5.3 (补丁) — fusion-identity 集成 (#88)
- [x] 新增 `auth/identity.py` — `IdentityClient` 同步 `httpx.Client` 调 `POST /api/v1/auth/verify` (header `Authorization: Bearer <FUSION_IDENTITY_SERVICE_TOKEN>`, body `{"token": "<user JWT>"}`); jti→claims 缓存 (TTL 60s, cap 1024); `revoked`/`tenant_status != active`/连接失败 → fail-closed (生产模式不静默回退本地 JWT); `emit_usage()` → `POST /api/v1/tenants/{tid}/usage` (best-effort)
- [x] 透明委托 seam — `get_default_verifier()` (jwt.py) 启用时返 `_IdentityJWTAdapter`, 所有消费方 (mcp_http `_auth_denied`/space api/rate_limit) 零改动获 identity 校验; `verify_any_token` (fallback.py) 门控静态降级: 启用 + `FUSION_REQUIRE_JWT=1` + 校验失败 → fail-closed; WS/TCP 路径 (remote/sync/collab_ws) 同 seam 覆盖
- [x] UDS `desk_rpc` (非 FastAPI 无中间件) — `_authenticate` Step 0 直调 `IdentityClient.verify()`, revoked/不可达 fail-closed, 保留 `set_current_tenant`
- [x] FastAPI 应用 (space/api, mcp_http ×2) — 采用 `fusion_core.tenant.install_tenant_middleware` (强制 `X-Tenant-Id` 头 + jwt.tid↔头匹配) + cowork bridge 中间件传播 `fusion_core.tenant.TenantContext` → cowork `get_current_tenant()` contextvar (双 contextvar 关键: fusion_core 源+强制, cowork bridge 传播, 非两个竞争强制器)
- [x] 配额来自 identity — `QuotaEnforcer(identity_client=...)` 从 verify 缓存读 `VerifyResponse.quota` (替代 ConfigCenter); `record_usage()` 上报到 fusion-identity (best-effort)
- [x] 开发降级保留 — 未启用 (或可达 + `FUSION_REQUIRE_JWT` 未设) → 现有 `verify_static_token`/本地 `JWTVerifier`/ConfigCenter 配额不变 (本地单机开发)
- [x] 纵深防御: 所有 `fusion_core.tenant`/`install_tenant_middleware` 导入在 `is_identity_enabled()` 守卫内 (启用时懒导入), 镜像 `mlx_client.py` 守卫导入模式; fusion-identity + fusion-core 加入 `[cloud]` extra; Postgres RLS 保留
- [x] opt-in 默认 OFF (镜像 guard #73/cluster #79 模式), 零行为变化, 1254 tests passed (29 新 identity 用例 offline httpx MockTransport), ruff 0 issues

#### V0.5.4 (补丁) — fusion-identity 生产契约文档 (#90)
- [x] 新增 `docs/identity.md` — 完整生产契约: opt-in 环境变量表 (FUSION_IDENTITY_ENABLED/_URL/_SERVICE_TOKEN + FUSION_REQUIRE_JWT)、部署拓扑 (fusion-identity 127.0.0.1:11470 loopback)、HTTP 契约 (Authorization: Bearer + X-Tenant-Id + 可选 X-User-Id, exempt 路径)、UDS 契约 (扁平 params._auth_token + x-user-id, tid 仅来自 verify 响应, 记录与 studio `_auth:{jwt,tid}` 嵌套的差异 + flatten 适配器)、WS/sync/remote 经 verify_any_token seam、配额来自 VerifyResponse.quota、禁用开发路径
- [x] 修正 README.md 过时声明 — fusion-core/fusion-identity 为 in-tree monorepo 包 (非 PyPI), 经 `pip install -e fusion-core`/`-e fusion-identity` 从 monorepo 根安装, 不经 extras (CI 单仓 checkout 故缺失, 中间件测试 skipif)
- [x] 文档补丁, 无代码变更; 1254 tests 不受影响

#### V0.2.9 (补丁) — 商用问题修复
- [x] P0: Computer Use 循环截图以 `image_url` 多模态格式传入模型 (原盲调用, 致盲修复)
- [x] P0: `ScreenCaptureNode` 视觉分析复用同一多模态通道
- [x] P1: `_applescript_move` 缺 cliclick 时明确告警 (替代静默失败)
- [x] P1: CDP `list_network_requests`/`list_console_messages` 落地后台 reader loop 真实事件缓冲 (原 `return []` 桩)
- [x] P2: 文档计数漂移修正 (节点 33→47 / 6→7 分类, MCP 15→16, Hook 11→14, SpaceAPI 18→25)
- [x] 测试: 2 个 P0 多模态回归用例, 全套 579 passed, ruff 0 issues

#### V0.2.10 (补丁) — 鉴权对接文档修正 (#46)
- [x] 修正 `FUSION_MLX_API_KEY` 说明: 应为 fusion-gateway client key, 非 mlx backend key (两套独立鉴权)
- [x] README/README_CN 新增双鉴权说明 + 401 排错指引
- [x] `FusionMLXClient.chat()` 401/403 错误增加可操作提示 (引导检查 key 来源)
- [x] 测试: 579 passed, ruff 0 issues

#### V0.1.9 (补丁)
- [x] 从 git 追踪中移除本地文档（6 个文件 → .gitignore）
- [x] feat: ast_diff 模块从 fusion-multi-node 迁移 (#26, #27)

#### V0.1.8 (补丁)
- [x] 端口规范化: 9760→11437, 9761→11438, 9762→11439 (#24, #25)
- [x] Ruff lint: 修复 desk_rpc 处理器中 4 个未使用变量

#### V0.1.7 (补丁)
- [x] 19 个缺失 RPC handler 补全，兼容 Studio GUI (#19, #23)
- [x] P0: desk.space.chat.stream — 流式对话
- [x] P1: agent.update, 快照 CRUD (5), 评论 CRUD (2)
- [x] P2: 工作流 (3), 发现扫描, 桌面共享 (2), 会话快照 (5)
- [x] Store 层: update_agent, remove_agent, 快照 get/delete, 评论 CRUD
- [x] RPC 方法总数: 79 → 99

#### V0.1.6 (补丁)
- [x] RPC 别名: `desk.space.chat.history` → chat.list (Studio 兼容, #20)
- [x] RPC 别名: `desk.space.notification.*` → notification.* (Studio 兼容, #21)
- [x] GUI 差距审计: 19 个缺失 RPC handler 已归档 (#19)

#### V0.1.5 (补丁)
- [x] start.sh 生命周期管理 (start/stop/restart/status/log/doctor/clean)
- [x] `python -m fusion_cowork` 现在可用 (__main__.py)
- [x] 移除 desk.mlx.start/stop — 产品层不应管理基础设施 (#16)
- [x] 修正 pyproject.toml 描述 (#16)

#### V0.1.4 (补丁)
- [x] CI: GitHub Actions 工作流 (ruff lint + pytest + coverage)
- [x] Lint: ruff 配置完成，F 系列缺陷已修复，0 问题
- [x] 测试: 519 项测试全通过
- [x] 文档: 环境变量说明、CI 徽章

### V0.8 (规划中)
- [ ] 可视化工作流编辑器 (Fusion-Studio GUI)
- [ ] 插件系统 (第三方节点包)
- [ ] 云备份与恢复 (可选，加密)
- [ ] 移动端伴侣 App (远程触发，通过 WebSocket)

---

## 🔒 安全与隐私

- **100% 本地离线** — 所有操作在本地执行，零文件上传
- **无遥测** — 无网络请求、无数据上报、无分析
- **预览模式** — 所有操作支持 `--dry-run` 预览
- **可撤销** — 删除操作默认移到废纸篓
- **完整审计** — 执行日志和复盘报告

---

## 🤝 参与贡献

欢迎贡献！请先阅读[贡献指南](CONTRIBUTING.md)。

---

## 📄 开源协议

本项目基于 Apache License 2.0 开源。详见 [LICENSE](LICENSE)。

---

## 架构影响来源

Fusion-Cowork 基于以下开源项目的模式构建：

| 模式 | 来源 | 整合位置 |
|------|------|----------|
| 工具注册表 + 类型强制转换 | [Squish](https://github.com/nicepkg/squish) `tool_registry.py` | `engine/node.py` — `_coerce_int/bool/number/array` |
| Lazy Import `__getattr__` | [Squish](https://github.com/nicepkg/squish) `__init__.py` | `fusion_cowork/__init__.py` — `_LAZY_IMPORTS` |
| 工具名称映射 | [Squish](https://github.com/nicepkg/squish) `tool_name_map.py` | `NODE_NAME_ALIASES` + `register_alias` |
| 内置工具集 | [Squish](https://github.com/nicepkg/squish) `builtin_tools.py` | 5 个工具节点：Shell、Python、Web 搜索、Fetch、Edit |
| 工作流引擎 (DAG) | [n8n](https://github.com/n8n-io/n8n) | `engine/workflow.py` — 拓扑排序、数据传递 |
| MCP 协议 | [LibreChat](https://github.com/danny-avila/LibreChat) | 计划 V0.2 实现 |

---

<p align="center">
  <strong>Fusion-Cowork — 让 Mac 自己干活，本地 AI 全自动桌面办公</strong>
</p>
<p align="center">
  <sub>Built with ❤️ by Fusion-MLX Team</sub>
</p>
