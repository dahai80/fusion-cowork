[English](README.md) | [中文](README_CN.md)

---

<div align="center">
  <h1>🧹 Fusion-Cowork</h1>
  <p><strong>Local-first, zero-code desktop automation platform for macOS Apple Silicon</strong></p>
  <p><em>Let your Mac do the work — 100% offline, AI-powered, privacy-first.</em></p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-brightgreen" alt="macOS">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License">
  <img src="https://img.shields.io/badge/AI-MLX%20Native-orange" alt="MLX">
  <img src="https://img.shields.io/badge/Offline-First-important" alt="Offline">
  <img src="https://img.shields.io/badge/status-beta-yellow" alt="Beta">
  <img src="https://img.shields.io/badge/version-0.5.4-blue" alt="Version">
  <img src="https://github.com/dahai80/fusion-cowork/actions/workflows/ci.yml/badge.svg" alt="CI">
</p>

---

## 📋 Overview

**Fusion-Cowork** is one of the three flagship products in the [Fusion-MLX](https://github.com/fusion-mlx) Apple Silicon local AI ecosystem. It provides a zero-code desktop intelligent automation platform for all office users.

### Ecosystem Positioning

| Product | Role | Audience |
|---------|------|----------|
| **Fusion-Code** | Developer coding agent | Programmers |
| **Fusion-Agent-Studio** | Advanced agent workflow orchestration | Developers / Architects |
| **Fusion-Cowork** 🎯 | Desktop automation for everyone | **All office users** |

> **Studio builds workflows, Desk runs them, Code writes capabilities, KB stores knowledge, Hub manages models.**

### Key Differentiators

- ✅ **100% Local & Offline** — Zero file uploads, zero privacy leaks, zero telemetry
- ✅ **macOS Native** — Deep integration with Desktop, Downloads, Documents directories
- ✅ **MLX Hardware Acceleration** — Apple Silicon native inference via fusion-mlx
- ✅ **AI Semantic Understanding** — Understands content, not just filenames
- ✅ **Zero Code** — Describe your need in plain language, get an automation workflow
- ✅ **Full Ecosystem Integration** — Seamless with Fusion-Code, Agent-Studio, Fusion-KB, Model-Hub

---

## 🚀 Quick Start

> **New to fusion-cowork?** Follow the **[Getting Started Guide](docs/guide.md)** — scenario-based walkthroughs (tidy your Desktop, AI-generate a workflow, batch docs, automate a site, schedule tasks, Claude Code MCP, collaboration, cloud deploy) with a decision table and troubleshooting tree. This README is the full command/node reference catalog; the guide is the on-ramp.

### Installation

```bash
# Clone
git clone https://github.com/dahai80/fusion-cowork.git
cd fusion-cowork

# Install with pip
pip install -e .

# Install with AI support (optional, recommended)
pip install -e ".[web]"

# Office deliverables (xlsx_write / pptx_write nodes, issue #151 #152)
pip install -e ".[office]"

# Harness kernel + plugins ecosystem (required at engine import, issue #196)
# Monorepo: the extra resolves (sibling fusion-plugins-ecosystem already installed).
# Standalone clone: the extra does not resolve (fusion-plugins-ecosystem is a
# monorepo sibling, not on PyPI) — install the kernel from PyPI instead:
#   pip install fusion-harness
pip install -e ".[plugins]"

# Or use uv (faster)
uv venv --python 3.11
uv pip install -e ".[test]"
```

> Note: a bare `pip install -e ".[test]"` installs, but running the full test
> suite additionally requires the fusion-harness kernel — install with
> `pip install fusion-harness` (or `pip install -e ".[plugins]"` inside the
> monorepo, where the sibling `fusion-plugins-ecosystem` is already installed).
> The suite imports `fusion_cowork.engine`, whose shims need fusion-harness;
> without it, collection fails with the actionable install hint
> (`engine/_harness_guard.py`, issue #196).

### Basic Usage

```bash
# List available templates
fusion-cowork template list

# Show template details
fusion-cowork template show desktop_daily_cleanup

# Run a template (preview mode)
fusion-cowork template run desktop_daily_cleanup --dry-run

# Run a template (execute)
fusion-cowork template run desktop_daily_cleanup

# Generate a workflow with AI
fusion-cowork ai generate "Organize all PDFs on my desktop by topic"

# Check AI service status
fusion-cowork ai status

# System information
fusion-cowork system info

# Start MCP server (stdio mode for Claude Code)
fusion-cowork mcp serve

# Start MCP server (HTTP mode)
fusion-cowork mcp serve --transport http --port 11438

# Start Desk RPC server (for Fusion-Studio GUI)
fusion-cowork desk rpc

# Lifecycle management via start.sh
./start.sh start    # Start desk RPC daemon
./start.sh stop     # Graceful stop
./start.sh restart  # Stop + start
./start.sh status   # PID, socket, memory, uptime
./start.sh log [-f] # Tail logs (-f to follow)
./start.sh doctor   # Health check (venv, CLI, socket, upstream services)
./start.sh clean    # Rotate logs, clear __pycache__
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FUSION_CLIENT_KEY` | _(unset)_ | **fusion-gateway client api key** (a key from `auth.api_keys[].key` in fusion-gateway `config.yaml`). NOT the fusion-mlx `settings.json` `auth.api_key` — gateway and mlx use separate auth. Resolved via `fusion_core.config.client_key()` (issue #204); legacy `FUSION_MLX_API_KEY`/`MLX_API_KEY` still honored as deprecated aliases by fusion-core. `FUSION_PROFILE=prod` + missing key = fail fast. |
| `FUSION_MLX_URL` | `http://localhost:11432/v1` | fusion-mlx base URL — full URL, highest priority (overrides host/port) |
| `FUSION_MLX_HOST` | `localhost` | fusion-mlx host (composed with `FUSION_MLX_PORT`; used only when `FUSION_MLX_URL` unset) |
| `FUSION_MLX_PORT` | `11432` | fusion-mlx port (composed with `FUSION_MLX_HOST`; used only when `FUSION_MLX_URL` unset) |
| `FUSION_MLX_MODEL` | _(unset)_ | default chat model for NL workflow generation (`NLWorkflowGenerator`). Pin a chat-capable model id to avoid picking `image_gen`/`video_gen` models (issue #85) |
| `FUSION_RAG_URL` | `http://localhost:11436` | fusion-rag (fusion-kb) base URL |
| `FUSION_IDENTITY_ENABLED` | _(unset)_ | opt-in: set `1` to activate fusion-identity as sole JWT issuer + tenant registry (issue #88). Default OFF = zero behavior change (local JWT/static-token dev path unchanged) |
| `FUSION_IDENTITY_URL` | `http://127.0.0.1:11470` | fusion-identity service base URL (only used when `FUSION_IDENTITY_ENABLED=1`) |
| `FUSION_IDENTITY_SERVICE_TOKEN` | _(unset)_ | service token sent as `Authorization: Bearer` to fusion-identity `/verify`. Required when enabled (else identity client stays None) |

> **base_url 优先级** (issue #83): `FUSION_MLX_URL` (整 URL) > `FUSION_MLX_HOST` + `FUSION_MLX_PORT` > 默认 `localhost:11432`。默认不变 → 多节点/gateway 部署字节级无影响。**本地单机直连 mlx** (绕过 gateway, 用 mlx key 而非 gateway key):
> ```bash
> export FUSION_MLX_HOST=127.0.0.1 FUSION_MLX_PORT=11434
> ```
>
> **鉴权说明**: fusion-cowork 通过 fusion-gateway(:11432) 调 fusion-mlx,存在**两套独立鉴权**:
> 1. **gateway client key** — 客户端请求 gateway 时用,即本变量 `FUSION_CLIENT_KEY`(取自 gateway `config.yaml` 的 `auth.api_keys[].key`; issue #204 前叫 `FUSION_MLX_API_KEY`, 别名仍兼容)
> 2. **mlx backend key** — gateway 转发到 mlx 时用,配置在 gateway `config.yaml` 的 `backends.fusion-mlx.api_key`,客户端无需关心
>
> 常见 401 错误:把 mlx 的 key 填进了 `FUSION_CLIENT_KEY`。应填 gateway 的 client key。本地单机直连 (上面 `FUSION_MLX_PORT=11434`) 时则相反 — 此时 `FUSION_CLIENT_KEY` 应填 mlx 的 key。

```bash
# Example: set API key to a fusion-gateway client key (from config.yaml auth.api_keys)
export FUSION_CLIENT_KEY="fg-admin-key"
fusion-cowork desk rpc

# Session management
fusion-cowork session list
fusion-cowork session show <session_id>
fusion-cowork session fork <session_id> --from-step 2
fusion-cowork session cleanup --days 30

# Permission management
fusion-cowork permission level <manual|auto|plan|bypass>
fusion-cowork permission approve <tool_name> --scope <scope>
fusion-cowork permission deny <tool_name> --scope <scope>
fusion-cowork permission list

# fusion-guard integration (issue #73, opt-in, default OFF)
# Set FUSION_GUARD_ENABLED=1 + ensure /tmp/fusion-guard.sock exists to activate.
# HIGH_RISK_NODES delegate to guard.evaluate (UDS JSON-RPC); low-risk nodes stay local.
# Guard unreachable -> cached rules fail-closed (deny high-risk). Optional shared secret
# via FUSION_GUARD_SHARED_SECRET. Confirm pending L3 approvals via desk.permission.confirm_guard.

# Benchmark
fusion-cowork benchmark report --format markdown|html|json [-o report.md]
fusion-cowork benchmark run --node file_input --node shell_exec --repeats 3

# Plugin management (M3)
fusion-cowork plugin list
fusion-cowork plugin install /path/to/plugin
fusion-cowork plugin load <name>
fusion-cowork plugin uninstall <name>

# Skill management (M3)
fusion-cowork skill list
fusion-cowork skill run /cleanup
fusion-cowork skill search "clean"

# Spreadsheet reconciliation — budget vs export-folder variance report (issue #151)
fusion-cowork skill run /spreadsheet-recon "~/fin/budget.xlsx ~/fin/exports 10"

# Markdown skill packages — frontmatter params render a form in the web UI (issue #155)
fusion-cowork skill packages
fusion-cowork skill run-package /weekly-readout --params '{"days": 7}'
fusion-cowork skill run-package /contract-review --params '{"contract_path": "~/contracts/a.md", "kb_id": "playbook"}'
# Package layout: YAML frontmatter (name/params/handler/output_template) + markdown instructions.
# Shared format (issue #167): JSON-Schema `parameters` + `allowed-tools` + `version` are understood by
# both fusion-cowork and fusion-agent-studio, so one pack works in either product — see docs/skill-packages.md.
# Built-ins ship in fusion_cowork/skills/packages/; drop your own *.md into ~/.fusion-cowork/skills/,
# or point FUSION_COWORK_SKILLS_DIRS=dir1:dir2 at shared directories.

# Chrome CDP — remote browser control (M3)
fusion-cowork cdp navigate https://example.com
fusion-cowork cdp snapshot
fusion-cowork cdp click 42
fusion-cowork cdp fill --selector "#search" --value "hello"
fusion-cowork cdp screenshot --save ~/Desktop/shot.png
fusion-cowork cdp evaluate "document.title"

# fusion-browser CDP shim target (issue #65, issue #77)
# FUSION_BROWSER_CDP=<port> -> drive the embedded fusion-browser shim on 127.0.0.1:<port> instead of external Chrome.
# The shim hardens its WS upgrade with a fail-closed Origin gate (E-15): the client MUST send an
# allowlisted Origin. Set FUSION_CDP_ORIGIN=<origin> to match the operator's allowedOrigins config
# (e.g. https://fusion.local). Without it the WS upgrade, Page.navigate and PUT /json/new are denied.
export FUSION_BROWSER_CDP=9223
export FUSION_CDP_ORIGIN=https://fusion.local

# Managed browser (issue #153) — spawn a dedicated Chromium profile with its own CDP port,
# no manual `--remote-debugging-port` setup. Workflow node: `managed_browser`
# (action launch/close/status, import_cookies=<Netscape cookies.txt>). Web: `fusion-cowork web serve`
# exposes POST /api/browser/launch|close and GET /api/browser/status|screenshot (live side panel).
# Binary discovery: FUSION_COWORK_CHROMIUM=<path> overrides Chrome/Chromium/Edge/Brave lookup.

# fusion-browser engine (issue #162) — managed launch of the fusion-browser UDS engine with a
# per-run config (never ~/.fusion-browser), a persistent per-run profile, honest login import
# counters, and a direct-UDS screenshot stream for the side panel. Workflow node: `fusion_browser`
# (action launch/create_session/import_cookies/execute/screenshot/finish/close). Web:
# POST /api/fusion-browser/launch|session|import-cookies|execute|finish|close,
# GET /api/fusion-browser/status|screenshot|stream (SSE). Binary override: FUSION_BROWSER_BIN.

# Collaboration Space (M6+M7)
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

# Mobile push notification (Bark/ntfy/本地降级) — V0.2.13
fusion-cowork push send "任务完成" "桌面清理已完成 12 个文件"
fusion-cowork push config --bark-url https://api.day.app --ntfy-url https://ntfy.sh

# Deep Research (多 Agent 研究) — V0.2.13
fusion-cowork research run "对比 MLX 与 llama.cpp 在 Apple Silicon 上的推理性能" --depth 3 --max-sources 3

# UltraReview (多 Agent 代码审查) — V0.2.13
fusion-cowork review run fusion_cowork/engine/workflow.py --lens security --lens correctness
fusion-cowork review run --diff          # 审查 git diff 变更集

# LSP 代码智能 (定义/引用/悬停) — V0.2.13
fusion-cowork lsp definition fusion_cowork/engine/workflow.py:120
fusion-cowork lsp references fusion_cowork/engine/node.py NodeRegistry
fusion-cowork lsp hover fusion_cowork/ai/mlx_client.py:45

# Worktree (git 隔离执行) — V0.2.13
fusion-cowork template run <id> --worktree

# MCP 导入 (add-from-Claude-Desktop) — V0.2.13
fusion-cowork mcp import ~/Library/Application\ Support/Claude/claude_desktop_config.json

# 远程控制 TLS + 命名会话 attach — V0.2.13
fusion-cowork remote serve --port 11439 --token mytoken --tls --tls-cert cert.pem --tls-key key.pem
fusion-cowork remote attach my-session --url wss://host:11439/control --token mytoken

# 技能持久化 + 用户自写 skill 包 — V0.2.13
fusion-cowork skill save /my-cleanup ~/skills/my-cleanup.json
fusion-cowork skill load ~/skills/my-cleanup.json

# 交互式对话 Agent Loop (白话→自主拆解多步→观察→决策→行动) — V0.2.14
fusion-cowork agent-loop run "整理下载目录里近 7 天的截图归档到桌面"
fusion-cowork agent-loop chat            # REPL 模式, 中途可 stop 叫停 / 补一句再续

# 实时光标 + 成员 presence/online_status — V0.2.14
curl -X POST localhost:8000/spaces/s1/presence/heartbeat -d '{"user_id":"u1"}'
curl -X POST localhost:8000/spaces/s1/presence/cursor -d '{"user_id":"u1","x":120,"y":80,"target":"art1"}'
curl localhost:8000/spaces/s1/presence

# 协作层 WebSocket 双向 (聊天/光标/presence 实时广播) — V0.2.14
fusion-cowork collab serve --port 11439
# 或 FastAPI WS 端点: ws://host:8000/spaces/{space_id}/ws (hello: {"user_id":..,"display_name":..})
```

---

## 🎯 Built-in Templates (10 ready-to-use)

| Template | Category | Description | Needs AI |
|----------|----------|-------------|----------|
| 🧹 **Desktop Daily Cleanup** | Desktop | Organize desktop files by type | ❌ |
| 📥 **Download Organizer** | File | Archive, deduplicate, and clean Downloads | ❌ |
| 📝 **Document Batch Summarizer** | AI | Batch summarize PDFs/Word/Markdown | ✅ |
| 🏷️ **AI Smart File Classification** | AI | Classify files by semantic content | ✅ |
| ✏️ **AI Batch Rename** | AI | Generate meaningful filenames from content | ✅ |
| 💾 **Disk Space Cleaner** | System | Scan and clean caches, temp files | ❌ |
| 📂 **Project File Collector** | File | Gather project files by type | ❌ |
| 🔍 **Duplicate File Scanner** | System | Find and clean duplicate files | ❌ |
| 📋 **Log Cleaner** | System | Scan and clean log files | ❌ |
| 🖼️ **Image Batch Rename** | File | Batch rename and classify images | ❌ |

### Industry Templates (V0.3)

| Template | Industry | Description |
|----------|----------|-------------|
| 🎨 **Design Asset Organizer** | Design | Auto-classify PSD/AI/Sketch files |
| 🖼️ **Design Batch Export** | Design | Batch export design assets |
| 💻 **Project Code Cleanup** | Dev | Clean caches, pycache, node_modules |
| 🔍 **Log Analyzer** | Dev | AI-powered log analysis |
| 🚀 **Project Scaffold** | Dev | Auto-create project structure |
| 📊 **CSV Data Cleaner** | Data | Deduplicate, normalize CSV |
| 🔄 **Format Converter** | Data | Batch CSV↔JSON↔Excel |
| 🖥️ **System Inspector** | Ops | Collect system health report |
| 💾 **Backup Manager** | Ops | Scheduled file backup |
| ⚠️ **Disk Alert** | Ops | Monitor disk usage, auto-clean |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────┐
│                     UI Layer                          │
│   CLI (click)  │  Web UI (FastAPI)  │  macOS (.app)  │
├─────────────────────────────────────────────────────┤
│                 Workflow Engine Layer                  │
│   WorkflowEngine  │  TaskScheduler  │  NodeRegistry   │
│   (DAG, n8n-inspired) │  (APScheduler) │  (68 nodes)   │
├─────────────────────────────────────────────────────┤
│                   AI Capability Layer                  │
│   FusionMLXClient  │  NLWorkflowGenerator             │
│   (HTTP → fusion-mlx)  │  (Natural language → Wf)     │
│   KBClient (HTTP → Fusion-KB)                        │
├─────────────────────────────────────────────────────┤
│                 System Capability Layer                │
│   macOS nodes (AppleScript / osascript)               │
│   File ops  │  Desktop mgmt  │  Disk cleanup  │ Tools  │
├─────────────────────────────────────────────────────┤
│              Collaboration Space Layer 🆕              │
│   SpaceStore (SQLite WAL)  │  SpaceService (CRUD)     │
│   SpacePermission (4-tier)  │  SpaceMemberService     │
│   SpaceChatService (SSE)   │  SpaceKBService (RAG)    │
│   SharedContext (nodes)     │  SpaceAPI (REST+SSE)     │
└─────────────────────────────────────────────────────┘
```

### Node Types (72 nodes built-in, 7 categories)

| Category | Count | Nodes |
|----------|-------|-------|
| `tool` | 38 | Shell Exec, Python REPL, Web Search, Fetch URL, Apply Edit, **Managed Browser** (issue #153 — dedicated profile + auto CDP port), **Fusion Browser** (issue #162 — managed fusion-browser engine, per-run profile, login import, panel stream), Browser Open/Extract/Automate, **CDP** (Navigate, Snapshot, Click, Fill, Fill Form, Screenshot, Evaluate, Emulate, Network, Console, Drag, Hover, Press Key, Type Text, Upload File, Wait For, Handle Dialog, Resize Page, Heap Snapshot, Lighthouse, List Pages, New Page, Select Page, Close Page, Performance Trace), **Push, LSP, Worktree** |
| `macos_system` | 13 | Desktop Clean, Download Organizer, Disk Cleaner, File Watcher, Screen Capture, Clipboard, Notification, App Lifecycle, OCR, Mouse Move, Mouse Click, Keyboard Type, Keyboard Shortcut |
| `file_operation` | 6 | File Classifier, Batch Rename, Copy, Move, Delete, Find |
| `ai_processing` | 4 | AI Classify, AI Summarize, AI Generate Name, AI Vision Analyze |
| `logic` | 3 | Filter, Loop, Merge |
| `fusion_ecosystem` | 4 | Trainer (Fusion-Trainer 互通), **Memory Commit, Memory Retrieve** (Fusion-Memory 互通), **Studio Run** (Fusion-Agent-Studio 互通, issue #133) |
| `io` | 4 | File Input, File Output, **xlsx_write** (multi-sheet Excel w/ formulas + conditional formatting, issue #151), **pptx_write** (editable PowerPoint from JSON spec + period-compare flagging, issue #152) |

### Claude Cowork Parity (V0.2) 🆕

| Capability | Status | Implementation |
|------------|--------|---------------|
| Screen capture & desktop view | ✅ | `ScreenCaptureNode` — full/selection/window screenshot |
| Clipboard read/write | ✅ | `ClipboardNode` — pbpaste/pbcopy |
| System notifications | ✅ | `NotificationNode` — macOS Notification Center |
| macOS app lifecycle | ✅ | `AppLifecycleNode` — launch/quit/activate/list |
| OCR / screen text recognition | ✅ | `OCRNode` — Vision + fusion-mlx |
| MCP protocol server | ✅ | `MCPServer` — 16 tools for Claude Desktop/Code |

### Ecosystem Integration

| Component | Protocol | Purpose |
|-----------|----------|---------|
| **fusion-mlx** | HTTP API (via fusion-gateway :11432) | LLM inference, text generation, embeddings |
| **Fusion-RAG** | HTTP API (port 11436) | Knowledge base semantic search, RAG |
| **Fusion-Code** | Auto-generated scripts | Complex logic execution |
| **Agent-Studio** | HTTP API (:11442) + workflow import | `studio_run` node submits graph/agent runs; advanced pre-built workflows |
| **Model-Hub** | Model dispatch | Auto-select optimal local model |

### Run-Scoped Execution Logs (v0.5.19, issue #120)

`WorkflowEngine` emits structured, run-scoped log lines so a failed run is diagnosable from logs alone — `grep "[<run_id>]"` reconstructs the full execution timeline:

```
[<run_id>] run start: workflow=<name> nodes=<n>
[<run_id>] step start: node=<node_type> node_id=<id>
[<run_id>] step end:   node=<node_type> verdict=<success|gated|blocked|failed> duration=<s> params=<sanitized> error=<...>
[<run_id>] run end:    outcome=<status> total=<s> steps=<n>
```

- **Verdict ≠ status** — node output flags (`blocked=true`, `passed=false`, `gated`/`requires_approval`) distinguish business gates (L2 hard-rule blocks, L3 approval gates) from errors; permission-denied steps log `verdict=gated`.
- **Sanitized params** — scalar values only, strings capped at 200 chars, complex values summarized (`<dict:N>`), secret-named keys (`*key*`/`*token*`/`*password*`/...) redacted, 20-key cap.
- **Log levels** — step start/end at INFO, failures and gates at WARNING, engine-internal errors at ERROR. `AgentOrchestrator` task execution follows the same `[<task_id>]` convention.

### Acceptance Batch Fixes (v0.5.20, issues #122–#127)

Six issues from the 2026-10-20 5-repo issue-completion acceptance, fixed in one batch:

- **#122 (P1) — Space store init on pre-v0.4 DBs**: tenant indexes moved out of `_SCHEMA_SQL` into `_TENANT_INDEX_SQL`, executed after `MigrationRunner.run_pending()` (v1 adds the `tenant_id` columns first). Legacy DBs no longer die with `no such column: tenant_id` on `_init_sqlite`/`_init_postgres`; `/api/space/*` works on upgraded installs.
- **#123 (P2) — `/api/deliver` wiring**: `web_serve` now injects a `CrossDeviceSync` when `FUSION_COWORK_CROSS_SYNC=1` (`FUSION_COWORK_SYNC_HOST/PORT/TOKEN/CERT/KEY` configure it; default off → honest 503 unchanged). `server/sync.py::cross_sync_from_env()` is the testable seam.
- **#124 (P2) — local CONFIRM approvals**: guard-OFF high-risk denials now record an entry in `PermissionManager._pending_local_approvals`; `list_pending()` merges guard + local entries (`source: guard|local`); `confirm_guard()` approves local entries without guard. `/api/permissions/pending` and `/api/mobile/overview` surface both sources — the Approvals page is no longer permanently empty in the default posture.
- **#125 (P2) — manifest upstream/api_key**: `web/manifest.json` declares `upstream` (`http://127.0.0.1:11461`) + `api_key_env`; `shell_registry.load_manifest()` resolves `api_key` from that env at registration time and honors `FUSION_COWORK_WEB_UPSTREAM` overrides. Gateway dynamic proxy (#232 contract) can resolve the module.
- **#126 (P3) — chat-only model catalog**: `/api/models` defaults to chat-capable models only (`engine_type` not in the non-chat set); `?all=1` returns the raw list; `total` carries the raw count. `_is_chat_model` is shared with `NLWorkflowGenerator._pick_chat_model`.
- **#127 (P3) — vendor sync + web tests**: vendored `@fusion/web-sdk`/`web-ui` synced byte-identical with upstream (fixes #19/#20 now present); `web/tests/sdk.test.ts` adds the first `bun test` suite covering the #96 runtime-key seam + #20 content-type contract (`bun run test`).

### Scheduler Executor Fix + Startup Port-registry Self-check (v0.5.50, issues #218/#219)

- **#218 — scheduled tasks actually run**: `_make_task_executor`'s closure took a `task_id` parameter, but `TaskScheduler._run_task` invokes executors with zero arguments (`await executor()`) — every cron/interval trigger raised `TypeError`, and after 5 consecutive failures the scheduler auto-FAILED the task. No run was ever created from the web console's schedule page. The executor is now zero-arg and fires the workflow via `RunRegistry.start` with a logged trigger line; a missing workflow definition fails visibly (ERROR log) instead of crashing the scheduler. Tests: `tests/test_issue_218_scheduler_executor.py` (fire creates a real run with `fail_count == 0`; executor signature has no required parameters).
- **#219 — port-registry self-check at startup**: `start.sh` now verifies its own `FUSION_PORT` (default 11438) is registered under `fusion-cowork:` in `architecture/port-registry.yaml` (`FUSION_REGISTRY` overrides the path) before starting — an unregistered port fails at `start` time with a clear error instead of being caught later by the CI `check-ports` lint. Missing registry file = warn + continue (standalone deployments); `FUSION_SKIP_REGISTRY_CHECK=1` bypasses explicitly. New `./start.sh registry-check` subcommand runs the check standalone (exit 1 if unregistered). Container mode is unchanged (no monorepo registry inside containers). Tests: `tests/test_issue_219_start_registry_check.py` (registered/wrong-port/missing-file/bypass/custom-port).
- **CI flake fix (main)**: `tests/test_issue_190_mailbox_consumers.py` push tests broke their polling loop on `calls`, then `pool.stop()` could cancel `_dispatch_one` mid-flight between `dispatch()` and the `ack`/`consumed` increment — on slow CI runners `consumed` stayed 0 (`assert 0 == 1`). Both tests now poll `pool.metrics["consumed"]` before stopping. Test bug, not pool bug; cancellation during shutdown is legitimate.

### HITL Reject-once Wakes the Run (v0.5.49, issue #216)

- **#216 — reject with `once: true` wakes the suspended run**: `POST /api/permissions/pending/{action_id}/confirm` with `{"approved": false, "once": true}` used to dequeue the pending card but never call `resolve_hitl` — control fell through to `confirm_guard`, which found no entry (already released) and returned `confirmed: false`, so the run hung at the HITL await until the 1800s timeout auto-denied it. The once branch now wakes the run on reject too (`resolve_hitl(exec, False)`), mirroring approve-once: the card is released, the node is DENIED, the run reaches a terminal state immediately, and **no deny rule is persisted** (the next run pauses again). Callers get one-time reject semantics without the old workaround's side effect — `once: false` rejects persisted a `{"allowed": false, "scope": "*"}` rule that insta-denied every future call of that node.

### MCP Registry-check Degraded-mode Warning (v0.5.48, issue #214)

- **#214 — visible degraded mode**: when `fusion_core` is not importable (bare CI install), the `mcp_client` registry check used to skip with only a DEBUG line — the audit silently disappeared. The skip branch now emits a WARNING (`注册表审计降级跳过`) carrying the target `url` + `tool`, once per process (no CI-loop log spam). In full fusion deployments fusion-core is a hard dependency, so the warning never fires. `docs/mcp-registry.md` documents the degraded-mode behavior.

### MCP Client JSON-string Arguments (v0.5.47, issue #211)

- **#211 — `mcp_client` accepts JSON-string `arguments`**: the node's own output is text (`data["result"]`), so an edge `{"source_output": "result", "target_input": "arguments"}` between two `mcp_client` nodes delivered a string and the downstream node failed with `arguments 须为 JSON 对象` before any HTTP call, making `mcp_client → mcp_client` chaining impossible. String arguments (from inputs or params) are now trimmed and `json.loads`-parsed; a valid JSON object is used as-is, invalid JSON fails with `JSON 字符串解析失败`, and valid JSON that is not an object keeps the original error. Non-string, non-dict values are unchanged.

### HTTP Request Node + Run Attribution + MCP Registry Check (v0.5.46, issues #206–#210)

- **#206 — generic `http_request` node** (`nodes/tools/http_request_node.py`): one node = one HTTP request (`method`/`url`/`headers`/`body`/`timeout`), so external apps can build DAGs that call their own REST APIs without a running MCP server. The body (and headers) support `${dotted.path}` templating resolved against the node inputs (which include run-level `inputs` injection); unresolved paths stay literal with a WARNING. Response JSON lands in the step output (`status_code`/`json`/`text`/`headers`) for downstream nodes; non-2xx maps to a visible node failure so run-status reconciliation works. HIGH_RISK node (arbitrary target + auth headers) with `url:` scope approval like `mcp_client`; intentionally no SSRF loopback blocking — this node is the intended path for sibling localhost services.
- **#207 — per-run source/labels**: `POST /api/runs` accepts optional `source` (string) and `labels` (string-to-string dict); both are validated (400 on bad types), stored on the run record, surfaced in `GET /api/runs` + `GET /api/runs/{id}` + the run-events broadcast, and included in the `PushEgress` `run_finished` and `approval_requested` payloads (as `run_source`/`run_labels` on approvals) so webhook receivers can route events by originating app. All additive — callers that omit the fields see zero change.
- **#209 — per-run inputs over HTTP**: `POST /api/runs` accepts optional `inputs` (alias `initial_input`) and forwards it as `message_input` to `RunRegistry.start` — same semantics as the mailbox dispatch path, so the first node receives run parameters without baking them into a per-run workflow-def copy. Non-dict values get a 400 instead of the old silent drop.
- **#210 — MCP server registry check on connect**: the `mcp_client` node calls `fusion_core.mcp_registry.check_approval(url, tool_scope=tool)` (fusion-core >= 0.9.0) before the `initialize` handshake. `denied`/`unknown` verdicts log a WARNING with the verdict reason + correlation id (the `fc_` observability trace id) but do not block — the HIGH_RISK `url:` scope operator approval remains the enforcement point. Registry unreadable or fusion-core absent (bare CI install): check skipped, no behavior change.
- **#208 — consumed-server registry & credential docs**: new [`docs/mcp-registry.md`](docs/mcp-registry.md) covers the approved-server inventory (`architecture/mcp-server-registry.json`, resolution order + mtime cache), per-scope operator approval, credential issuance for consumed servers (`identity_issuance` / `env_ref`, "no self-carried keys", Sheets-server example), warn-level enforcement, and the correlation-id audit contract.

Tests: `tests/test_issue_206_http_request_node.py`, `tests/test_issue_207_run_labels.py`, `tests/test_issue_209_runs_inputs.py`, `tests/test_issue_210_mcp_registry_check.py`.

### Node Executors & Parallel Scheduling (v0.5.40, issue #189)

- **#189 — `NodeExecutor` protocol** (`fusion_cowork/engine/executors.py`): the engine no longer calls `node.execute()` directly; it resolves a per-node executor (`executor_for_node`). `SimpleExecutor` (default) forwards to `node.execute()` — zero behaviour change for existing workflows. `AgentExecutor` hands the task text to a fusion-agent-studio agent and reports the real multi-turn loop (`turns`, `tool_calls`, `tokens`).
- **#189 — per-node executor config**: `NodeConfig` gains `executor`, `agent`, `tools`, `model`, `max_turns`, `budget_tokens` (all serialised by `to_dict()`/`from_dict()`), so one DAG can mix deterministic nodes with agentic ones. Unknown executor names log a WARNING and fall back to `simple`.
- **#189 — agent transport**: UDS `subagent.run`/`subagent.transcript` on `/tmp/fusion-studio.sock` (`FUSION_AGENT_STUDIO_SOCKET`) honours every per-node override; the HTTP channel (`FUSION_AGENT_STUDIO_URL`, default `:11442`) cannot, so it logs an explicit degradation WARNING instead of pretending. Missing daemon → visible node failure, never a silent plain-LLM fallback.
- **#189 — ready-set wave scheduler**: nodes whose predecessors are done run in the same wave, bounded by concurrency. Resolution order: `execute(concurrency=N)` > `FUSION_COWORK_MAX_CONCURRENCY` > `1`. Concurrency `1` is byte-for-byte the historical serial topological loop. `start.sh` exports `FUSION_COWORK_MAX_CONCURRENCY=4` so deployed services are parallel by default while the library default stays serial.
- **#189 — determinism under concurrency**: steps are appended in topological order (not completion order) before progress notification, so REST/SSE/step logs stay identical between serial and parallel runs. Directives (`set_params`/`insert_node`/`skip_remaining`) apply at wave boundaries against the first unexecuted node; HITL approval is serialised per run via `_RunState.hitl_lock`.
- **#189 — validation**: isolated (unconnected) nodes are no longer a validation error — they log `工作流含无连线节点, 将作为独立分支执行` and run as independent branches, which is what makes "N independent nodes in parallel" expressible.
- **#189 — observability**: `WorkflowStep.executor`, `WorkflowExecution.concurrency`/`executors`, and run start/end log lines carrying `concurrency=... executors={'simple': 2, 'agent': 1}` plus the same mix in the final summary.

Tests: `tests/test_issue_189_node_executor.py` (33 cases + 1 live-AS skip: wave parallelism wall-clock/overlap/peak, env resolution, config roundtrip, UDS success/partial/failure/transcript-tolerance, HTTP completed/failed/timeout/override-degradation, mixed-DAG e2e, agent failure semantics, resume across an agent node). Docs: `docs/node-executors.md`.

### Durable Run State Recovery (v0.5.38, issue #191)

- **#191 — `RunStateStore`** (`fusion_cowork/engine/run_persistence.py`): SQLite persistence of run control state at `<store_root>/run_states.db` (default `~/.fusion-cowork/web/run_states.db`). One row per **active** run (`running`/`paused`); terminal rows are deleted when the run ends, stale terminal rows are cleaned with a WARNING at load. Schema version via `PRAGMA user_version` — opening a database written by a newer fusion-cowork raises `RuntimeError` at startup, never a silent downgrade read.
- **#191 — event-as-checkpoint**: every engine event (`RunRegistry._on_event`) persists the full record — workflow def, step snapshots, queued directives, status, `paused_reason`. Persist failures only WARN; persistence is a side path and never blocks execution.
- **#191 — single recovery path**: `RunRegistry.recover_persisted()` runs on FastAPI startup. Each active row is rebuilt as `paused` and its engine task restarts with `start_paused=True` + `resume_steps=<snapshots>` — the same resume path as in-process pause/resume, so step-level progress and control state recover through one code path. Persisted directives are replayed into the new engine's directive queue and take effect at the first node boundary after resume. Unparseable rows are logged at ERROR and deleted, never silently retried.
- **#191 — crash detection is loud**: a row persisted as `running` means the previous session died without cleanup (`kill -9`, crash). Recovery logs a WARNING and restores the run as `paused` with `paused_reason="crash"`. Recovered runs **never auto-resume** — they wait for a human `POST /api/runs/{id}/resume` or a fresh HITL approval.
- **#191 — back-compat**: `RunRegistry` without an injected `run_store` stays pure in-memory (zero persistence); `engine.execute()` receives `resume_steps`/`start_paused` only when set, so injected legacy engines keep working. Recovered runs skip memory-bridge re-injection (the first snapshot already carries the original context). Details: `docs/run-recovery.md`.

Tests: `tests/test_issue_191_durable_run_state.py` (8 cases: store roundtrip, stale-terminal loud cleanup, schema-version gate, kill -9 crash recovery into paused + HITL re-entry + snapshot-skip, directive replay after recovery, manual-paused row recovers as manual, corrupt row discarded loudly, `create_web_app` store wiring).

### HITL Default ON + Claim Hygiene (v0.5.37, issues #187/#188)

- **#187 — HITL pause-on-pending is now the DEFAULT**: `FUSION_COWORK_HITL_PAUSE` semantics flipped — the engine pauses high-risk nodes at the node boundary for human approval unless the env is explicitly set to `0`/`false`/`off`/`no` (headless deny-fast, the pre-v0.5.37 behavior). **Migration for unattended operators**: set `FUSION_COWORK_HITL_PAUSE=0` to keep deny-fast semantics; any other value (or unset) now pauses and waits.
- **#187 — bounded wait**: `FUSION_COWORK_HITL_TIMEOUT` (default `1800` seconds; accepts `s`/`m`/`h` suffixes) bounds every HITL wait. On timeout the gated node is auto-denied (never silently proceeds), the stale pending entry is discarded from the approval queue, a `hitl_timeout` notification hook fires, and the run takes the existing DENIED path.
- **#187 — `completed_with_denials` terminal state**: runs whose denied nodes had `continue_on_error=true` now finish with `completed_with_denials` instead of plain `success` — delivery is visible as "completed but with denials" in the run list (web `statusKind` maps it to warning).
- **#187 — approve-once**: `POST /api/permissions/pending/{action_id}/confirm` accepts `"once": true` — the pending entry is released and the paused run wakes (`resolve_hitl(..., once=True)` grants a one-shot pass, no permission rule persisted). Default (no `once`) still persists the rule via `confirm_guard`.
- **#187 — web approval card**: run detail shows an awaiting-approval card while `hitl_pending` — node name, sanitized params, live countdown from the server-reported `hitl_deadline`, and three actions: Approve once / Always allow (persist rule) / Deny. Pending state lives server-side, so a page reload restores the card; the 2s poller keeps it live.
- **#188 — false parallelism claims purged**: `engine/workflow.py` module/class docstrings no longer claim parallel execution — the main loop is serial topological (and now says so). Capability claims carry `CLAIM: verified by tests/<file>.py` tags, and `scripts/check_claims.py` (wired into the CI lint job) fails on dangling references or `engine/workflow.py` dropping below its minimum CLAIM coverage.

Tests: `tests/test_issue_187_hitl_default.py` (21 cases: env semantics flip, `_parse_hitl_timeout` units, timeout auto-deny + queue cleanup + deadline pop, `completed_with_denials`, non-high-risk unaffected, approve-once no-persist round trip, discard helper, web confirm-once + `hitl_deadline` + reload durability); `tests/test_issue_181_hitl_pause.py` default assertions updated; `tests/conftest.py` pins the suite headless.

### MCP Client Node (v0.5.36, issue #185)

- **First-class `mcp_client` node** (`nodes/ecosystem/mcp_client_node.py`): workflows can call external MCP tool servers over the Streamable HTTP transport — one node = one tool call. The node performs the full three-step sequence (`initialize` handshake → `notifications/initialized` → `tools/call`), reuses the server-issued `Mcp-Session-Id` header on subsequent requests, parses both JSON and SSE-framed (`text/event-stream`) JSON-RPC replies, forwards custom `headers` (e.g. auth), and surfaces `isError` results as visible node failures. This closes the cowork-as-MCP-server / cowork-as-MCP-client gap and removes the `python_repl` escape hatch from routine DAGs.
- **URL-scoped permissions**: `mcp_client` is a HIGH_RISK node (remote tool-execution surface) and `Permission.matches` gains a `url:` scope prefix — `approve("mcp_client", scope="url:http://127.0.0.1:11483/**")` authorizes exactly one service without opening arbitrary targets or blanket code execution. Chinese aliases: `MCP工具` / `MCP调用` / `调用MCP`.
- Registered via `import_all_nodes()` (13 node modules now); `fetch_url`'s loopback SSRF guard no longer matters for sibling-service calls — this node is the intended path for them.

Tests: `tests/test_issue_185_mcp_client_node.py` (11 cases: full protocol sequence with session-header assertions, JSON + SSE reply parsing, `isError` failure path, connection failure visibility, missing/invalid params fail-fast, custom header forwarding, registration, HIGH_RISK listing, url-scope permission matching unit + manager level, unapproved deny).

### Engine Reliability Batch (v0.5.35, issues #180/#181/#182)

- **#180 — python_repl multi-statement fix**: the subprocess wrapper consumed stdin twice — `eval(sys.stdin.read())` drained the pipe, so the `SyntaxError` fallback re-read an empty stream and multi-statement code silently did nothing while reporting success. The wrapper now reads stdin once into `_src`, eval-compiles first and exec-compiles on `SyntaxError`; user syntax/runtime errors are reported as node failures instead of a silent empty success.
- **#182 — retry observability**: `WorkflowStep` gains `attempt` (1-based final attempt number) and `attempt_errors` (one entry per failed attempt); both are surfaced in the run steps API (`_step_dict`). `NodeConfig` gains an opt-in `retry_on_business_failure` flag (default off) — business `FAILED` results participate in engine retries only when the node opts in; exceptions/timeouts retry exactly as before. `to_dict`/`from_dict` round-trip the new flag.
- **#181 — HITL pause-on-pending**: opt-in engine mode (constructor `hitl_pause=True` or `FUSION_COWORK_HITL_PAUSE=1`) — when a high-risk node lands in the local pending-approvals queue, the run pauses at the node boundary (reusing the W8 pause event; `cancel()` still wakes it) instead of failing deny-fast. `POST /api/permissions/pending/{action_id}/confirm` now reads the pending entry's `execution_id`, and after persisting the rule it wakes the paused run (`engine.resolve_hitl`) which re-executes only the gated node with its prior inputs — approved runs resume in place (no replay of already-executed nodes), rejected runs follow the existing DENIED/compensation path. `GET /api/runs/{id}` reports `hitl_pending` while waiting. Guard-side (fusion-guard L3) pendings have no execution_id and keep the deny-fast behavior.
- **Test-isolation hardening**: web-api auth tests now stub `_module_key()` — a locally issued gateway module key (`~/.fusion-cowork/web_module_key`) no longer flips "no key configured" contract tests into 401.

Tests: `tests/test_issue_180_repl_stdin.py` (6 cases), `tests/test_issue_182_retry_observability.py` (7 cases), `tests/test_issue_181_hitl_pause.py` (7 cases incl. full web confirm-route wiring).

### Heartbeat Manifest Convergence + Shell Theme Following (v0.5.34, issues #176/#177)

- **#177 — heartbeat-carried manifest**: fusion-gateway #284 (`handleShellManifestHeartbeat`) accepts an optional manifest body — a body naming a manifest triggers `HeartbeatWithManifest` and converges stale ext fields (pages/searchProviders/notifications); an empty body stays a plain liveness ping. The module side now adopts the contract: `_registry_loop_body` re-reads `web/manifest.json` from disk on every beat and POSTs it as the heartbeat body, so on-disk edits (add/remove a page) converge into the gateway registry within one heartbeat interval without a restart. A failed re-read (missing/corrupt file) falls back to the registration-time copy so liveness never breaks; the fm- key auth path (401 → re-register) is unchanged.
- **#176 — iframe follows the shell theme toggle**: new `web/src/theme.ts` — embedded mode subscribes `sdk.theme.onThemeChange` (the shell posts `{type:"event", event:"theme"}` and re-pushes on iframe load, so no flash) and toggles `html.light`/`html.dark` (tokens.css class-based palette); standalone mode resolves the initial theme from `localStorage["fusion.shell.theme"]`, then `prefers-color-scheme`, then dark, and persists every change back. Wired in `main.tsx` before first paint.

Tests: `tests/test_issue_177_registry_heartbeat.py` (4 cases: heartbeat body carries the manifest, on-disk edit converges within one beat, re-read failure falls back to the registration copy, 401 re-register arm intact); `web/tests/theme.test.ts` (6 cases: light/dark class toggling, stored-preference/garbage/prefers-color-scheme/no-storage initial-mode chain).

### Registry Key Extraction + Models Polling Fix (v0.5.33, issues #172/#173/#174)

- **#173/#174 — gateway-issued key never persisted**: fusion-gateway's K1 issuance response (`writeShellKeyResponse`) returns the plaintext module key under `raw_key`, but `_extract_issued_key()` only looked for `api_key`/`issued_key`/`key`/`module_key` — the one-shot `fm-` key was silently dropped and `~/.fusion-cowork/web_module_key` never written. `raw_key` is now the first candidate (top level and nested `data`); legacy field names stay for older/custom gateways. Heartbeats now authenticate with the issued key as designed.
- **#172 — `/api/models` 503 polling storm**: while the model client is unavailable (cold cache, upstream down, or no client injected), the web UI's fixed-interval poller retried the bare 503 every few seconds, spamming the shell tab console. The route now returns an honest `200` envelope — `{"models": [], "count": 0, "stale": false, "unavailable": true}` — so the poller succeeds once and renders an empty state instead of retrying. The #147 stale-cache path (last success served with `stale: true`) is unchanged; `web/src/api.ts` types the new `unavailable` field.

Tests: `tests/test_issue_172_173_174_registry_models.py` (6 cases: `raw_key` extraction top-level/nested/precedence + gateway-shaped response persisted through the registry loop, unavailable envelope for missing client, envelope shape under `?all=1`); updated contracts in `tests/test_issue_146_147_web_auth_models.py` and `tests/test_web_p3.py`.

### Extensions Console: Skills + Knowledge (v0.5.32, issue #170)

Migrates the f-ecommerce Extensions console (E2, insight/f-ecommerce-migration-1022.md) — the Skills and Knowledge tabs, same layout and interactions, data sources moved to this module's own endpoints:

- **Skills tab** — JSON user packs (`~/.fusion-cowork/skills/<name>/skill.json`, handler `workflow|node|script`) with Validate & install / Test-run / Remove. Installing saves the pack through cowork's own persistence API (`skills/persistence.py`), registers it into the shared `SkillRegistry` (capability `skill.<name>`, callable via MCP `skill_run`), and the pack handler now honors the install directory for script packs. Script packs (arbitrary code) are admin-only: allowed for the local operator (no auth configured) or the static `FUSION_COWORK_WEB_API_KEY`, refused for gateway-issued `fm-` module keys.
- **Knowledge tab** — fusion-rag console reusing the SpaceKB `KBClient` binding (`:11436`, `FUSION_RAG_URL` override): health marker, KB create & wire, filterable + paginated KB list with document counts, document upload & ingest (`.md`/`.txt`, 10 MB cap, staged under `~/.fusion-cowork/web/rag_uploads/`), and a search panel returning citation-relevant fields (`text`/`doc_path`/`score`). Wire state persists to `~/.fusion-cowork/web/rag_wired.json` and surfaces as the `knowledge.<kb_id>.search` capability; unwiring reports the disabled capability id.
- **Web** — new `#/extensions` page (`ExtensionsPage.tsx`, `web/manifest.json` page entry) with the tabbar + tables + install/test/KB panels mirroring the source page; `api.ts` gains the `/api/extensions/*` client surface.

Tests: `tests/test_issue_170_extensions_console.py` (22 cases: pack validation/install/registry registration/script admin gate/test-run honest envelope/remove, RAG health/filter/pagination/wire/documents/upload/search against a fake KBClient).

### Shared Skill Format + Gateway-Issued Module Keys (v0.5.31, issues #167/#168)

- **Shared markdown skill format (#167)** — one pack now serves both fusion-cowork and fusion-agent-studio. `parse_markdown` accepts the shared frontmatter: JSON-Schema `parameters`/`arguments` (`properties`/`required`, mapped to the cowork form fields; `enum` now validated on run), `allowed-tools`/`tools`, `command`/`slash`, `version`, and hyphenated aliases; unknown product-specific keys (`handler`, `workflow`, `params`) stay cowork-only and never break the agent-studio loader (its `parse_skill_md` parks them in `extra`). The seed `/contract-review` pack ships in the shared format. `FUSION_COWORK_SKILLS_DIRS` (colon-separated) adds shared install directories so a single file installs once for both products. Spec: `docs/skill-packages.md`. Tests: `tests/test_issue_167_shared_skill_format.py` (21 cases).
- **Gateway-issued module keys (#168, K4)** — the shell registration body no longer sends a module-supplied `api_key` unless `FUSION_COWORK_WEB_API_KEY` is explicitly set (static override kept for old gateways). When the gateway (K1) returns an issued `fm-` key in the registration response, cowork persists it once to `~/.fusion-cowork/web_module_key` (0600, plaintext never logged — masked `fm-x****xxxx` only) and authenticates heartbeats with it instead of the admin key; a 401 heartbeat falls back to re-registration. Inbound `/api` auth dual-accepts the env static key and the issued key during the 24 h grace window, so fresh installs need zero manual key handling and existing installs upgrade without downtime. Migration note (one line): keep `FUSION_COWORK_WEB_API_KEY` set through the upgrade; once the gateway issues a key, the persisted `~/.fusion-cowork/web_module_key` takes over and the env var can be dropped. Tests: `tests/test_issue_168_gateway_keys.py` (15 cases).

### fusion-browser Managed Backend (v0.5.30, issue #162)

Implements the cowork half of fusion-browser#25 (`fusion-browser/docs/cowork-managed-backend-contract.md`):

- **Managed launch** — `fusion_cowork/fusion_browser.py` spawns the fusion-browser engine with `FUSION_BROWSER_CONFIG` pointing at a per-run config (socket, random auth token, `tokenCapabilities: ["all"]`, explicit `allowedOrigins`), waits for the socket to answer `metrics`, and SIGTERMs on teardown. The operator's `~/.fusion-browser` is never read or written.
- **Per-run profile** — `create_session(profile_id="run-<id>")` keeps login state in a persistent engine profile; run→profile mapping is persisted to `~/.fusion-cowork/fusion-browser/profiles.json` so a resumed run re-attaches; `finish` closes sessions and deletes the profile unless `keep_profile`.
- **Login import** — optional `import_cookies(session_id, browser, profile)` returns the engine's honest counters (`imported`, `skipped_app_bound`, `skipped_other`) and the UI shows them verbatim — never a blanket "logged in".
- **Panel stream** — a second direct-UDS connection runs `stream_screenshots`; `GET /api/fusion-browser/stream` re-emits `screenshot_frame` as SSE and treats `stream_end` as terminal. The web `BrowserPanel` gains a fusion-browser mode rendering the stream.
- **Node wiring** — `fusion_browser` node (TOOL, high-risk) exposes launch/session/import/execute/screenshot/finish over the same managed engine; `cdp_*` nodes are untouched.

The upstream Python client is vendored byte-identical under `fusion_cowork/vendor/fusion_browser_client/` (MIT, no PyPI package).

Tests: `tests/test_issue_162_fusion_browser.py` (13 cases: isolated config + env, launch rollback, session/profile persistence, honest cookie counters, execute mapping, finish teardown, stream frames, node + API/SSE surfaces against a fake UDS engine).

### Managed Browser, Markdown Skill Packages, Cross-Device Takeover, Admin Console (v0.5.29, issues #153/#154/#155/#156)

- **Managed browser (#153)** — `fusion_cowork/managed_browser.py` + the `managed_browser` node spawn a dedicated Chromium/Chrome/Edge/Brave process with an independent `--user-data-dir` (`~/.fusion-cowork/browser-profile`) and an auto-allocated `--remote-debugging-port`; `FUSION_BROWSER_CDP` is set on success so every existing `cdp_*` node drives the same session with zero manual Chrome setup. Optional `import_cookies` ingests a Netscape `cookies.txt` via `Network.setCookies` (login state stays on-device). `web serve` adds `POST /api/browser/launch|close`, `GET /api/browser/status|screenshot`; the web `BrowserPanel` streams screenshots every 2 s while a run executes. `FUSION_COWORK_CHROMIUM` overrides binary discovery.
- **Markdown skill packages (#155)** — `fusion_cowork/skills/packages.py` discovers `*.md` packages (YAML frontmatter: `name`, `params`, `handler`, `output_template`, `aliases`; body = instructions) from the built-in `skills/packages/` and `~/.fusion-cowork/skills/`. Parameter schemas drive the `#/skills` form in the web UI (`GET /api/skills`, `POST /api/skills/{name}/run`) and render structured forms; handlers can delegate to a built-in skill (`builtin:/spreadsheet-recon`), run a parameterized DAG, or call a local Python handler. Seed skills: `/contract-review` (contract + optional fusion-kb playbook hits + local MLX memo with clause citations), `/spreadsheet-recon`, `/weekly-readout` (lifecycle JSONL aggregation). CLI: `skill packages`, `skill run-package`.
- **Cross-device takeover + push egress (#154)** — `WS /ws/takeover/{run_id}` attaches an authenticated device to a live run (snapshot frame, run-event pump, `directive`/`pause`/`resume`/`cancel`/`confirm` frames); `PushEgress` posts `run_finished` and pending-approval events to `FUSION_COWORK_PUSH_WEBHOOK_URL` (best-effort, unset = no-op); MobilePage gains pause/resume/cancel and allow/deny controls.
- **Admin console + OTel audit export (#156)** — `/api/admin/*` behind `FUSION_COWORK_ADMIN_KEY` (503 when unset, 401 on mismatch): members, usage, push test, and `GET /api/admin/audit/export` emitting OTLP/JSON `LogsData` from the audit chain + terminal runs. New `#/admin` page keeps the admin key in localStorage only.

### GUI Audit Fixes: Block-Level Diff Review, Token Waterfall, Live Delegate Preview (v0.5.28, issues #142/#143/#144)

Implements the fusion-all-gui-audit-1021 §3.3/§3.4/§3.7 acceptance targets:

- **Block-level diff review (#142)** — edit-type pending approvals (`apply_edit`-style `old_text`/`new_text` or `old_string`/`new_string` params) render as SequenceMatcher diff blocks with per-block **Accept/Reject** toggles; "Allow once" with rejected blocks sends `accepted_blocks`, the backend rebuilds narrowed edit params via `apply_block_selection` (rejected hunks fall back to the original lines) and echoes `edited_params`; rejecting every block refuses the confirm without adding a rule. New text-diff engine in `engine/ast_diff.py` (`text_diff_blocks` / `apply_block_selection` / `unified_diff_text`) wired into the approval chain. File artifacts surface as unified diff cards: edit-pair step inputs and `diff` outputs produce `type: "diff"` artifacts (plus `.diff`/`.patch` files), rendered with +/- line coloring in the run detail artifact pane. The permission check now sees effective params (`config.params` merged with upstream input) so config-param nodes no longer show empty approval cards.
- **Token waterfall (#143)** — `WorkflowStep.tokens` extracted from node output `usage` (`_extract_tokens`: prompt/completion/total); AI nodes (classify/summarize/rename) aggregate cross-batch/cross-file usage into their output; `_step_dict` exposes `tokens`; RunsPage waterfall shows a per-row `tok` cell, the run header shows `Σ N tok`, and denied (blocked) nodes get a dashed border + 被阻塞 chip + amber bar.
- **Live delegate preview (#144)** — DelegatePage auto-generates the DAG preview after an 800 ms input debounce (≥6 chars, stale-response seq guard); the preview card is editable in place (workflow name/description, per-node label + params JSON editor, node delete with edge cleanup, edge delete/add) and only commits on Save.

Tests: `tests/test_issue_142_approval_diff.py` (15 cases: diff block split/selection rebuild, pending details + engine-run integration, accepted_blocks confirm narrowing/all-reject/full-accept, diff artifact sources) + `tests/test_issue_143_run_tokens.py` (5 cases: token extraction, engine capture, `_step_dict` compat, classify usage aggregation).

### GUI/UX Upgrade: Approval Cards, Waterfall, Space Instructions (v0.5.27, issue #138)

Implements the fusion-all-gui-1020 §6 targets for cowork (G6/G7/G10 + patterns C2/C3/C6):

- **Approval cards + rule persistence** — `web serve` now loads permission rules at startup (`PermissionManager.load()`) and saves after every confirm, so approve/deny rules survive restarts (previously in-memory only: deny → approve → rerun on every boot). `confirm_guard()` accepts a `scope` — the ApprovalsPage cards offer **three actions**: Deny / Allow once (precise scope derived from the params snapshot, e.g. `command:git` or `file:/path/**`) / Always allow (scope `*`). Local pending entries now carry a card context — sanitized params (scalars, 200-char cap, secret-key redaction), `ts`, plus `workflow`/`node_id` written by the engine via an approval ContextVar at the permission-check call site. Diff-style blocks render `command` and `old_string`/`new_string` params.
- **Run waterfall** — `RunRegistry._step_dict` exposes `input_data`; RunsPage detail renders per-node waterfall rows (status/duration bar ∝ execution_time); clicking a row expands a drawer with full input/output/error.
- **Space knowledge layer** — `SpaceConfig.instructions` (stored in the existing `spaces.config` JSON column, no migration); `GET/PUT /api/space/spaces/{id}/instructions`; `SpaceChatService` injects `[空间指令] <text>` as the leading system message in stream chat and both agent paths; SpacePage knowledge tab gains an instructions editor.
- **Delegate polish** — preview card + save-to-canvas jump already shipped (v0.5.8 W7); chat-filter model picker from #126/#131.

Tests: `tests/test_issue_138_gui_upgrade.py` (10 cases: save/load roundtrip, scoped confirm, pending card context, engine context + waterfall `input_data`, instructions API roundtrip + chat injection helper).

### Shell Manifest Ext Fields (v0.5.26, issue #139)

`web/manifest.json` now declares the fusion-web-shell#24 extension fields so cowork surfaces in the shell host's ⌘K palette, notification center, and sidebar deep links:

- **`pages`** — 7 module-relative routes (`/workflows` `/delegate` `/canvas` `/runs` `/sessions` `/spaces` `/approvals`) with zh/en titles; host deep-links `${entry}#${route}`.
- **`searchProviders`** — one provider (`cowork`) at `/shell/fusion-cowork/api/search`; `GET ?q=` aggregates workflow definitions (route `/canvas/<id>`), terminal runs (`/runs`), and spaces (`/spaces`), case-insensitive substring match, empty query → empty items, cap 32.
- **`notifications`** — two poll sources: `approvals` (`/shell/fusion-cowork/api/notifications/approvals`, wraps `list_pending()` — one item per pending approval, `level: warning`, stable `action_id` ids) and `runs` (`/shell/fusion-cowork/api/notifications/runs`, terminal runs newest-first, `level: error` for failures, stable `run_id` ids). Item ids are stable across polls per the host dedup contract (`${moduleId}:${sourceId}:${itemId}`).

Tests: `tests/test_issue_139_manifest_ext.py` (15 cases: manifest schema conformance + search/notification shapes, ids, caps).

### Runs API Malformed-Edge 400 Fix (v0.5.25, issue #135 F-2)

F-2 of #135 was **this repo's** union-web API, not agent-studio (initial mirror filing there was closed as misattributed): `POST /api/runs` with edges like `{"from": "n1", "to": "n2"}` crashed with 500 `KeyError: 'source_id'` inside `Workflow.from_dict`.

- **`Edge.from_dict`** — missing/blank `source_id`/`target_id` now raise a readable `ValueError` naming the missing keys (aligns with the existing cycle-detection `ValueError` precedent); `Workflow.from_dict` edge parsing routes through it (removes an inline duplicate of the same logic)
- **`runs_create`** — catches `ValueError` from `Workflow.from_dict` (malformed edges, cycles) → `400` with `工作流定义无效: <reason>` instead of 500

Tests: `tests/test_issue_135_runs_edge_400.py` (9 cases: Edge/Workflow validation + API 400/202).

### Studio Graph Execute 422 Fix (v0.5.24, issue #135)

Live cross-module acceptance of #133 found `studio_run` `graph_id`+`mode=sync` failing every time with `agent-studio HTTP 422` — upstream `GraphExecuteRequest` (`agent_runtime/api_server.py`) declares `graph_id` as a **required body field** (redundant with the path param, but enforced). `_execute_graph` now sends `{"graph_id": ..., "input_text": ...}`; wire-contract test asserts the body field (F-2, the `/api/runs` malformed-edge 500, is an agent-studio-side defect — mirrored upstream).

Tests: `tests/test_issue_133_studio_node.py` (18 cases).

### Agent-Studio Run Node (v0.5.23, issue #133)

Cross-module composition: a cowork DAG can now invoke a fusion-agent-studio graph/agent as a step. `StudioRunNode` (`fusion_cowork/nodes/ecosystem/studio_node.py`, node name `studio_run`, aliases `运行Studio`/`Studio运行`, ecosystem category now 4 nodes / 68 total):

- **Paths** — `graph_id` + `mode=sync` → `POST /v1/graphs/{id}/execute` (full `events` inline); otherwise `POST /v1/runs` submit — `mode=async` returns `run_id` immediately for upstream orchestration, `mode=sync` polls `GET /v1/runs/{id}` to a terminal state (`completed`/`failed`/`canceled`) then fetches `GET /v1/runs/{id}/result`. Sync timeout best-effort cancels via `POST /v1/runs/{id}/cancel` and fails visibly with the `run_id`.
- **Config** — env `FUSION_AGENT_STUDIO_URL` (default `http://127.0.0.1:11442`), optional `FUSION_AGENT_STUDIO_API_KEY` sent as `x-api-key` (upstream auth is optional in local-trusted mode). Params: `graph_id`/`agent_id` (exactly one), `input`, `mode`, `timeout` (300s), `poll_interval` (2s), `title`, `idempotency_key`. Outputs: `run_id`, `status`, `result`.
- **Gating** — `studio_run` added to `HIGH_RISK_NODES` (remote multi-step agent execution, same explicit-approval class as `fetch_url`); guard JSON fallback covers the content mapping.

Tests: `tests/test_issue_133_studio_node.py` (18 cases, httpx MockTransport offline stub).

### Chat-Model Filter Hardening (v0.5.22, issue #131)

Cross-repo alignment with fusion-gateway#245 (gateway now emits `engine_type` + `modality` + `capabilities` on `/v1/models`). Two gaps closed in `_is_chat_model` (`fusion_cowork/ai/nl_parser.py`, shared by `/api/models` and `_pick_chat_model`):

- **`draft` engine type filtered** — speculative-decoding draft models (e.g. `Qwen3.8-27B-DFlash2`) are added to `_NON_CHAT_ENGINE_TYPES`, so they can no longer be picked as the chat default.
- **`capabilities` as a second signal** — when the model entry carries gateway `capabilities` metadata (top-level or `metadata.capabilities`), it is consulted before the `engine_type` gate: `capabilities.embedding=true` → non-chat even without `engine_type` (e.g. bge-m3 with `modality=text`), `text_generation=false` → non-chat, `text_generation=true` → authoritative chat. Entries without `embedding`/`text_generation` keys fall through to the existing `engine_type` gate unchanged.

Tests: `tests/test_issue_131_chat_model_filter.py`.

### Shell-Embedded API Base (v0.5.21, issue #129)

Under the unified shell the module is served at `/shell/<id>/` — origin-root `/api/...` paths 404ed (gateway has no generic `/api/*` routes). The API layer now derives the module prefix from the document pathname (mirrors agent-studio's `apiBase()`):

- **`web/src/sdk.ts::apiBase()`** — `/shell/fusion-cowork/runs` → `/shell/fusion-cowork`, standalone root → `""`. Applied to standalone `directRequest`, `streamSse`, and the embedded postMessage bridge path (the host fetches the path as-is, so the prefix rides on it). One build serves both topologies.
- **Contract tests** — `web/tests/sdk.test.ts` gains an `apiBase` describe block (prefix derivation, prefixed direct request, no prefix at standalone root).

---

## 🕸️ Union Web Module (v0.5.5, issue #94; hardened v0.5.6, issues #96/#97/#98; P0 pages v0.5.7; P1 delegation v0.5.8)

### Standalone auth & gateway registration (v0.5.6, issues #96/#97)

- **Runtime key seam**: in a standalone release build the API key resolves at runtime — `window.__FUSION_COWORK_WEB_API_KEY__` > `localStorage['fusion-cowork-web-api-key']` > build-time `VITE_API_KEY` (legacy). No key → headers omitted (keyless serve posture, 127.0.0.1 bind). The standalone HTTP wrapper always sets `content-type: application/json` on bodied requests (upstream fusion-web-shell#22).
- **Gateway shell registration**: `web serve` registers `web/manifest.json` with fusion-gateway and keeps a heartbeat when both `FUSION_SHELL_GATEWAY_URL` + `FUSION_ADMIN_KEY` are set (`FUSION_SHELL_MANIFEST` overrides the manifest path; admin key resolved via `fusion_core.config.admin_key()`, issue #204). Unset → fully skipped. Mirrors agent-studio's `shell_registry`.

DAG workflow canvas in the browser — fusion-cowork joins the unified web shell (PRD N-6).

- **`web/` UI package** — React 18 + Vite + TS + Tailwind (preset from `@fusion/web-ui`) + Bun; `@xyflow/react` canvas. Workflow list (run/edit/delete with typed confirm), desktop DAG canvas editing (node palette from `/api/nodes`, connect edges, per-node params editor with schema hints), runs view with live polling (5s visible / 30s hidden-throttled), mobile read-only (`devices: ["desktop", "mobile-readonly"]`). Embedded mode (`?embedded=1`) hides own nav — shell owns chrome.
- **`fusion-cowork web serve`** — Web API on :11461: nodes catalog, workflow definition CRUD (atomic JSON store), run lifecycle (async submit → poll → cancel over the shared `WorkflowEngine`), template catalog (`/api/templates`, builtin bodies with workflow snapshot), scheduled tasks (`/api/scheduler/tasks` CRUD + pause/resume, backed by the engine `TaskScheduler`; `web serve` starts it so cron/interval tasks fire on the uvicorn loop). Serves `web/dist` statically for gateway `/shell/fusion-cowork/` hosting. Auth: `FUSION_COWORK_WEB_API_KEY` (gateway proxy injects; unset = allow, bind 127.0.0.1).
- **P0 pages (v0.5.7, migration doc §6)**: Nodes browser (W2, search + category colors + params schema), Templates catalog (W5, one-click "open in canvas" copies the template body into a user workflow), Schedule management (W6, create cron/interval tasks, pause/resume/delete), Runs status filter (W4), Canvas `?view=1` share-link read-only mode (W3).
- **P1 delegation & sessions (v0.5.8)**: NL delegate entry (W7 — natural-language prompt → `/api/ai/generate-workflow` via `NLWorkflowGenerator` → preview node draft → save to canvas / save & run), unified session list (W11 — `/api/sessions` over the engine `SessionStore`). `create_web_app` gains `nl_generator=` / `session_store=` injection seams. W8 run redirection + W9 delivery review need new interaction protocols (separate batch); W10 artifacts are space-scoped and land with the P2 space surface.
- **P2 space surface (v0.5.9)**: Spaces page (W12 — sidebar list/create/delete + detail pane), space chat with 5s polling (W13, `sendSpaceMessage` as `local_user`; SSE upgrade left for later), members tab (roles from space membership), artifacts tab (W10 — list/create/preview via new `/api/space/spaces/{id}/artifacts` routes on the mounted sub-app). `create_space_api` gains `artifact_svc=` param; `create_web_app` gains `space_api=` / `space_data_dir=` seams (default `_build_space_stack` under `~/.fusion-cowork/spaces`; mounted at `/api/space` behind the same static-key auth). `web serve` closes the `SpaceStore` on shutdown.
- **P2.5 space completion (v0.5.10)**: KB panel (W14 — status + bind/unbind + retrieval search + document list over the existing `/api/space/.../kb/*` routes; live search needs fusion-kb :11436), notification center (W15 — top-bar bell with unread count 30s poll + dropdown mark-read over new `GET /notifications` + `POST /notifications/{id}/read` on the mounted sub-app; `create_space_api` gains `notification_svc=` param wired to `NotificationService` in `_build_space_stack`), members-permission matrix (W16 — role `<select>` + remove per member over new `PUT /spaces/{id}/members/{user_id}` (auth + `manage_members` gate) and read-only 15-action × 4-role matrix via `GET /spaces/{id}/permissions`; existing `DELETE .../members/{user_id}` hardened to take the operator from the trusted principal instead of a query param, A-5/CR-5).
- **P3-1 ops surface (v0.5.11)**: model catalog (W17 — `GET /api/models` over `FusionMLXClient.list_models()`, 503 when fusion-mlx offline; delegate page gains a model dropdown persisted in `localStorage` and passed as optional `model` override to `/api/ai/generate-workflow` which builds a per-request generator without mutating shared state), plugins panel (W20 — new page over the existing `rpc_bridge` delegation: web API gains `POST /rpc` JSON-RPC 2.0 endpoint routing `plugins/*` (non-plugins → -32601, missing `fusion-plugins-ecosystem` → -32603, parse error → 400/-32700); plugins list with install/uninstall actions). W19 guard approvals / W18 quotas / W21 cluster / W22 mobile delivery still pending.
- **P3-2 approvals + cluster (v0.5.12)**: guard approval queue (W19 — `PermissionManager.list_pending_guard()` snapshot + `GET /api/permissions/pending` + `POST /api/permissions/pending/{action_id}/confirm` (confirm lands only when fusion-guard is enabled; `create_web_app` gains `permission_manager=` seam and injects the manager into the default engine)), cluster panel (W21 — read-only `GET /api/cluster/state` over the distributed state layer: node registry with heartbeat status + vRAM ledger by plugin; `cluster_enabled: false` when `FUSION_CLUSTER_ENABLED` unset). W8/W9 (interaction protocols), W18 quotas, W22 mobile delivery remain.
- **P4 delivery review + usage (v0.5.13)**: artifact review state machine (W9 — `SpaceArtifactService.review_artifact()` owner/admin approve/reject persisted to `metadata.review` (no schema change), `POST /api/space/spaces/{id}/artifacts/{aid}/review`; approve/reject buttons + status badge on the artifacts tab), usage/quotas indicator (W18 — `GET /api/usage` aggregating tenant counts (spaces/messages/artifacts/agents) + QuotaEnforcer quotas, UsagePage table). Remaining: W8 run redirection, W22 mobile delivery.
- **W22 mobile read-only + deliver (v0.5.14)**: `GET /api/mobile/overview` (recent runs + pending guard approvals snapshot), `POST /api/deliver` (run → desktop over the cross-device sync engine; `create_web_app` gains `cross_sync=` seam — 503 when not injected, honest `delivered: false` for offline/unknown devices, broadcast semantics otherwise), `#/m` MobilePage (run cards + deliver button + pending approvals, read-only). Remaining: W8 run redirection (orchestrator interaction protocol).
- **W8 run redirection (v0.5.15)**: engine-level pause/resume — `WorkflowEngine.pause/resume/is_paused` holds the execution at the next node checkpoint (asyncio.Event per execution; current node completes, the next waits), `cancel` wakes a paused execution; `RunRegistry.pause/resume` track `status: paused` and `POST /api/runs/{id}/pause|resume` return 409 on non-running/non-paused states; RunsPage gains pause/resume buttons (`paused` badge + status filter). Append-instruction redirection needs the orchestrator interaction protocol (I2) — out of scope. Migration doc §6 complete (W1–W22).
- **Shared harness kernel (v0.5.16)**: issue #109 migration — `engine/hooks.py` and `engine/permission.py` are now fusion-harness shims (mirroring fusion-agent-studio #357): kernel types (`HookEngine`/`HookConfig`/`HookResult`, `PermissionEngine`/`PermissionRule`/`PermissionDecision`/`PermissionMode`) are re-exported when the harness is installed, with `KERNEL_SOURCE` tracking the active source; in-tree `_hooks_local.py`/`_permission_local.py` copies preserve cowork-specific semantics (14-event `HookEvent`, priority ordering, fail-closed `PRE_*`, `HookContext.modified_data`, 5-level `PermissionLevel`, `HIGH_RISK_NODES` guard delegation, `confirm_guard`, atomic save/load). Install via `pip install -e ../fusion-harness` + `.[harness]`; absent harness falls back to the local copies (CI-safe).
- **Funnels + compactor adoption (v0.5.17)**: issue #111 — `engine/compactor.py` and `engine/funnels.py` are fusion-harness shims (kernel types re-exported when installed, `KERNEL_SOURCE` tracks the active source, verbatim in-tree fallbacks keep CI green). Real consumption: `SpaceChatService` routes every stream/agent LLM call through `LLMFunnel` (lifecycle `llm_before`/`compact_before`/`compact_after`/`llm_after` + `Compactor` with a 9000-token window matching the 24000-char budget, `_trim_context` as final guard), and `EventEmitter.attach_lifecycle_bus()` mirrors workflow events into a process-wide `LifecycleBus` (in-memory ring; `FUSION_COWORK_LIFECYCLE_DIR` persists JSONL for replay). `engine/lifecycle.py::get_lifecycle_bus()` is the shared singleton wired in the CLI runtime and space chat.
- **Run interaction protocol + real-time surface (v0.5.18, issues #113–#118)**: run directives at node boundaries (`WorkflowEngine.add_directive` — `set_params` defaults to the next node, `insert_node` supports `after_node_id` + injected input, `skip_remaining` marks the remainder `SKIPPED`; queued via `POST /api/runs/{id}/directive`, audited by `TrajectoryRecorder.record_directive`); run-level delivery review (`review` `pending_review` on success, `POST /api/runs/{id}/review` approve/reject, reject returns the workflow definition for iteration; distinct from W19 guard node approvals); run artifacts preview (`GET /api/runs/{id}/artifacts` derives text artifacts from step outputs — `file_path`/`output_path`/`path`, 64 KB cap, 20 items, inline markdown/html/text fallback); unified sessions (`GET /api/sessions?source=engine|space|scheduler` merges the three sources with `source`/`title`/`space_id`/`space_name`/`href`, sorted by `updated_at`); real-time channels (`GET /api/runs/{id}/events` SSE snapshots + engine `EventEmitter` broadcast, space `/stream` now shares the chat emitter and emits a `ready` frame; frontend uses fetch-based SSE because `EventSource` cannot send the runtime API key, polling retained as fallback and in embedded mode); global model picker in the top bar (`web/src/modelPref.ts`, persisted in `localStorage['fc-preferred-model']`, shared with the DelegatePage inline picker, forwarded to `/api/ai/generate-workflow` and space agent chat via the new `model` field on `POST /api/space/spaces/{id}/messages`).
- **Union-web compliant**: manifest per PRD §5.1, vendored `@fusion/web-sdk`/`@fusion/web-ui` (byte-identical to shell repo), four-state views (skeleton/empty/error/retry), token colors only, no native alert/confirm, no auth logic in UI.
- **Dev ports registered**: 11461 (web API), 11464 (vite dev) — `architecture/port-registry.yaml`.

```bash
# Backend
fusion-cowork web serve                     # :11461 (REST + static hosting)

# Frontend (dev)
cd web && bun install && bun run dev        # :11464, proxies /api → :11461
bun run build                               # dist/ → served by web serve / gateway
```

---

## ☁️ Multi-tenant Cloud SaaS (v0.4.0)

Fusion-Cowork v0.4.0 extends the local-first desktop platform into a **multi-tenant cloud SaaS** — same engine, cloud-ready isolation/auth/observability/deploy. All cloud features are **opt-in** (env/config set → activate; unset → unchanged local behavior).

### Tenant Isolation

- **Row-level `tenant_id`** column on all 14 store tables + query guards (`WHERE tenant_id = ?`) on every CRUD path
- **Postgres RLS** as defense-in-depth (`CREATE POLICY tenant_isolation USING (tenant_id = current_setting('app.tenant_id'))`)
- **Single schema**, single `SpaceStore` class with backend seam: pass `dsn`/`FUSION_PG_DSN` → Postgres (asyncpg pool); pass `data_dir` → SQLite (tests/local)
- 9 cross-tenant IDOR points sealed (delete_message, list_comments, get_invite, list_spaces, notifications, sessions, space CRUD, messages)

### Authentication (JWT)

- **PyJWT** verifies externally-issued **HS256/RS256** tokens, extracts `tenant_id`/`user_id`/`scopes` claims → `TenantPrincipal`
- **Static token fallback** retained for local/desktop (`auth.static_token`); production enforces JWT (`FUSION_REQUIRE_JWT=1`)
- JWKS remote fetch for RS256 (optional cache); failures log without leaking token plaintext
- ConfigCenter secrets **redacted in logs** + **encrypted at rest** (Fernet, `FUSION_ENCRYPTION_KEY`)

### Persistence & Migrations

- **SQLite → PostgreSQL** migration (asyncpg connection pool, MVCC)
- **Versioned migrations** (`schema_migrations` table, `MigrationRunner` — ordered apply, rollback on failure)
- **Backup/restore** via `pg_dump` wrapper — `fusion-cowork db backup|restore`
- Placeholder normalization helper (`?` → `$1,$2,...`) keeps 30 SQL sites backend-agnostic

### Observability

- **Structured logging** (structlog JSON, `setup_logger(json=True)`) + trace_id contextvar propagating across `await`
- **OpenTelemetry** metrics (`fusion_requests_total`, `fusion_request_duration_seconds`, `fusion_active_connections`, `fusion_db_errors_total`) + tracing (OTLP exporter) + prometheus `/metrics`
- **Deep `/health`** — checks DB (`SELECT 1`), disk threshold, upstream MLX/KB reachability → `{status, checks}`
- **SIGTERM graceful drain** — stop accepting, drain in-flight, close store/pool

### Security at Scale

- **fusion-guard integration** (issue #73) — `HIGH_RISK_NODES` delegate authorization to `guard.evaluate` via UDS JSON-RPC (`/tmp/fusion-guard.sock`); low-risk nodes stay local (no per-node IPC). Opt-in: `FUSION_GUARD_ENABLED=1` + socket present. `CONFIRM`/L3 → `guard.confirm` (pending store); L4 → block. Guard unreachable → cached rules (`guard.rules.dump` → `~/.fusion-guard/rules-cache.json`) fail-closed (deny high-risk), not fail-open. Optional `FUSION_GUARD_SHARED_SECRET`. Default OFF — zero behavior change.
- **CDP Origin allowlisting** (issue #77) — fusion-browser's CDP-over-WS shim hardened its Origin gate to fail-closed (E-15): an empty `Origin` is rejected, an empty `allowedOrigins` denies everything except local schemes (`data:`/`about:`/`blob:`). The CDP client sends an allowlisted `Origin` header on the WS upgrade via `FUSION_CDP_ORIGIN` (must match the operator's `allowedOrigins` config). Clears Bearer (H-5, issue #72) but does not clear Origin (E-15) — the two gates are independent.
- **Per-tenant rate limiting** (token bucket, `FUSION_RATE_LIMIT_*`)
- **Per-tenant quotas** (`TenantQuotas`: max_spaces/messages/artifacts/agents/storage, default unlimited)
- **Tamper-evident audit log** (sha256 chained `prev_hash`, `verify_chain`)
- **Upstream circuit breaker** (MLX/KB — `failure_threshold`/`recovery_timeout`, degrade instead of hang)
- **WS connection caps** (global + per-tenant) + collab session idle eviction (ResourceWarning leak fix)
- **Config secrets encrypted at rest**

### Deployment

- **Dockerfile** (multi-stage, nonroot, HEALTHCHECK) + **docker-compose** (app + postgres:16)
- **Helm chart** (`deploy/helm/fusion-cowork/`): Deployment (liveness/readiness probes, `terminationGracePeriodSeconds`, preStop), Service, Secret (JWT/PG DSN), ConfigMap, HPA (CPU 70%, 2–10 replicas)
- **`--container` flag**: binds `0.0.0.0`, skips UDS/venv detection — container-friendly
- `FUSION_CONFIG_DIR` env overrides config location (volume mount)

### Plugin Ecosystem Hardening

- **Ed25519 manifest signing** (`plugins/signing.py`) — `signature` field, canonical bytes, `verify_any_key` against configured public keys, `require_signing` opt-in
- **Plugin registry** (`plugins/registry.py`) — persistent `registry.json`, **downgrade rejection** (version tuple compare), checksum
- Sandbox isolation unchanged (process-out, rlimit) — signing is additive

### Install Cloud Extras

```bash
pip install -e ".[cloud,web]"   # pyjwt, asyncpg, cryptography, prometheus-client, opentelemetry, structlog
pip install -e ".[plugins]"     # fusion-plugins-ecosystem integration panel
```

### CI

5-job matrix: `lint` (ruff check + format) · `test-sqlite` (3.11/3.12/3.13 × ubuntu/macos) · `test-postgres` (postgres:16 service) · `test-slow` (load/E2E/chaos) · `security` (pip-audit). Coverage gate + SBOM workflow (CycloneDX + pip-audit artifact).

## 🌐 Distributed State Layer (v0.5.0)

Opt-in cross-node shared state for multi-node / macOS MLX cluster deployments (issue #79). **Default OFF** — zero behavior change when unset.

- **`DistributedStateStore`** (`fusion_cowork/distributed_state.py`) — atomic file-backed (JSON) cross-process store. Thread + coroutine safe (`threading.Lock` + `asyncio.Lock`), atomic `temp`+`os.replace` write, corrupt-file rebuild. Serializes: cluster node registry, vRAM allocation ledger, plugin enable/installed sets.
- **Cluster-aware handle wrappers** — `ClusterNodeRegistry` merges local `NodeRegistry` + peer nodes from the shared store; `ClusterTaskScheduler` adds `dispatch_with_failover()` (best-node selection by free vRAM + tags, cycles candidates on failure). Injected into `DeskRuntime` when cluster enabled.
- **vRAM ledger** — `record_vram_allocation` / `can_allocate_vram(limit_mb)` enforce a cluster-wide budget so N nodes loading MLX models can't collectively exceed physical unified memory (prevents swap avalanche).
- **Plugin-state sync** — `record_plugin_state` / `is_plugin_enabled_anywhere` make a plugin enabled on node A visible to node B's `plugins/states`.

Enable:

```bash
export FUSION_CLUSTER_ENABLED=1
export FUSION_CLUSTER_NODE_ID=node-a        # unique per node
export FUSION_CLUSTER_STATE_PATH=/shared/cluster-state.json  # shared volume / NFS
```

> **Note:** `DeskRuntime`'s *internal* state (`vram_allocations` / `_mcp_sessions` / `registered_plugin_ids`) is unreachable through the injected handles — that consumer-side serialization is tracked upstream at [fusion-plugins-ecosystem#13](https://github.com/dahai80/fusion-plugins-ecosystem/issues/13). This layer provides the shared store + handle wrappers the consumer will be wired into.


---

## 🔐 fusion-identity Integration (v0.5.3)

Retire the local JWT/tenant/RBAC/quota reimplementation in favor of **fusion-identity** as the sole JWT issuer + tenant registry for the ecosystem (multi-tenant PRD §3/§4, issue #88). **Default OFF** — opt-in, matches the guard (#73) / cluster (#79) pattern; zero behavior change when unset, all existing tests green.

- **`IdentityClient`** (`fusion_cowork/auth/identity.py`) — sync `httpx.Client` `POST /api/v1/auth/verify` (header `Authorization: Bearer <FUSION_IDENTITY_SERVICE_TOKEN>`, body `{"token": "<user JWT>"}`). jti→claims cache (TTL 60s, cap 1024) cuts repeat calls; `revoked`/`tenant_status != active`/conn-fail → fail-closed (no silent fallback to local JWT in prod mode). `emit_usage()` → `POST /api/v1/tenants/{tid}/usage` (best-effort).
- **Seamless delegation** — `get_default_verifier()` (jwt.py) returns an `_IdentityJWTAdapter` when enabled, so every consumer (mcp_http `_auth_denied`, space/api, rate_limit) gets identity-backed verify with no call-site change. `verify_any_token` (fallback.py) gates static-token dev fallback: enabled + `FUSION_REQUIRE_JWT=1` + verify failed → fail-closed (no static fallback in prod). WS/TCP paths (`remote.py`/`sync.py`/`collab_ws.py`) covered by the same seam.
- **UDS `desk_rpc`** (non-FastAPI, no middleware) — `_authenticate` calls `IdentityClient.verify()` directly (Step 0), fail-closed on revoked/unreachable, keeps existing `set_current_tenant`.
- **FastAPI apps** (space/api, mcp_http ×2) — adopt `fusion_core.tenant.install_tenant_middleware` (enforces `X-Tenant-Id` header + jwt.tid↔header match) + a cowork bridge middleware that propagates `fusion_core.tenant.TenantContext` → cowork's `get_current_tenant()` contextvar (the dual-contextvar crux: fusion_core sources+enforces, cowork bridge propagates — not two competing enforcers).
- **Quotas from identity** — `QuotaEnforcer(identity_client=...)` reads `VerifyResponse.quota` from the verify cache (replaces ConfigCenter); `record_usage()` emits to fusion-identity (best-effort).
- **Dev fallback retained** — disabled (or reachable + `FUSION_REQUIRE_JWT` unset) → existing `verify_static_token` / local `JWTVerifier` / ConfigCenter quotas unchanged (local-first single-machine dev).

Enable:

```bash
export FUSION_IDENTITY_ENABLED=1
export FUSION_IDENTITY_URL=http://127.0.0.1:11470
export FUSION_IDENTITY_SERVICE_TOKEN=<service-token>
# prod mode (no static fallback):
export FUSION_REQUIRE_JWT=1
```

> **Production contract & defaults:** see [`docs/identity.md`](docs/identity.md) — env defaults, deploy topology, and the HTTP (`Authorization: Bearer` + `X-Tenant-Id`) / UDS (`_auth_token` flat string) header contract fusion-studio and other clients should use.
>
> **Defense-in-depth:** all `fusion_core.tenant` / `install_tenant_middleware` imports live inside `is_identity_enabled()` guards (lazy at enable-time), mirroring `mlx_client.py`'s guarded-import pattern — if fusion-core is ever absent, the identity path silently no-ops rather than breaking import. Postgres RLS kept (unchanged). `fusion-core` / `fusion-identity` are in-tree monorepo packages (not on PyPI); install via `pip install -e fusion-core` / `-e fusion-identity` from the monorepo root, not via extras (CI does a single-repo checkout, so they are absent there — middleware tests skipif).

---

## 🧠 Run-Level Memory Integration (v0.5.29, issue #157 / fusion-memory#20)

Runs now close the loop with **fusion-memory** (the ecosystem long-term memory hub) automatically — no need to hand-place `memory_commit`/`memory_retrieve` nodes in the DAG. Task outcomes enter fusion-memory as short-term entries and get promoted to long-term by reuse (three-tier scheduling + forgetting curve are engine-side, already shipped).

- **Run start (retrieve)** — when memory is on, the run queries fusion-memory with `<workflow name> <description>` and injects the recalled context into the first node's input as `memory_context` (via `initial_input`). The run record exposes the injected text as `memory_context`.
- **Run end (commit, success only)** — the execution's steps become a fm-core `Interaction` (one turn: task = user message, result summary = assistant message, steps = tool calls, capped at 64 calls / 4000 chars per field) and are committed via fm-server `commit`. The run record exposes `memory_writeback` (`ok`/`failed`) + `memory_ids`.
- **Per-run switch** — `POST /api/runs` body `{"memory": false}` (default `true`), `desk.workflow.run` params `memory`, and a "Memory" checkbox in the web canvas run bar + delegate page. Off = zero RPC calls, no trace.
- **Zero-impact degradation** — `FUSION_MEMORY_API_KEY` unset → integration fully off (legacy behavior). fusion-memory down/unreachable → run still succeeds; failures surface fail-visible in the run record (`memory_writeback: "failed"`) + warn logs. Offline hard constraint kept: HTTP only to 127.0.0.1.

Implementation: `fusion_cowork/memory_bridge.py` (wire contract aligned with `fm-core` interaction.rs + fm-server `commit` P1-1 result shape). Tests: `tests/test_memory_bridge.py`, `tests/test_run_memory.py` (offline, MockTransport) + `tests/test_memory_e2e.py` (gated e2e vs real fm-server).

Enable:

```bash
# fm-server (fusion-memory) must be running on 11435 with HTTP auth
export FUSION_MEMORY_API_KEY=<fm-server api key>
export FUSION_MEMORY_BASE_URL=http://127.0.0.1:11435   # optional, default shown
```

Run-level off switch (API):

```bash
curl -X POST http://127.0.0.1:11463/api/runs -H 'Authorization: Bearer <key>' \
  -d '{"workflow_id": "wf1", "memory": false}'
```

Gated e2e (needs a real fm-server + fusion-mlx):

```bash
FUSION_MEMORY_E2E=1 FUSION_MEMORY_API_KEY=<key> pytest tests/test_memory_e2e.py -v
```

---

## 📬 Team Messaging — Durable Mailbox + Resident Consumers (v0.5.39, issue #190)

Cross-process, cross-project agent messaging without any external RPC pull. Cowork runs a **daemon-resident consumer pool**: every agent registered in the roster consumes its own inbox, dispatches the message to a workflow, and writes the result back to the sender as a reply.

- **Durable mailbox** — messages live in SQLite (`~/.fusion-cowork/team.db`), not in an in-memory queue. Unacked messages survive daemon restarts; `ack` only happens **after** dispatch returns a run id, so a crash redelivers (at-least-once).
- **Schema-compatible with agent-studio** — the `mailbox` table is byte-identical to `agent_runtime/durable_roster.py`. Point both daemons at the same file (`FUSION_TEAM_DB` / `FUSION_COWORK_TEAM_DB`) and cowork agents talk to agent-studio agents directly. Cowork keeps its own roster in a separate `cowork_roster` table so the two daemons never steal each other's rows.
- **Push + poll** — local sends wake the pool instantly (`TeamMailbox.on_send`); remote daemons can `POST /api/team/notify`; a configurable poll (`FUSION_INBOX_POLL_INTERVAL`, default 5s) covers peers with no notify hook.
- **No manual pull** — no `desk.*` RPC call is needed to move a conversation forward. The pool starts with `web serve` whenever the roster has an enabled agent (`FUSION_COWORK_RUNNER_POOL=1` forces it).
- **Observable** — `GET /api/team/status` reports live consumers, per-agent backlog, oldest unacked age, consumed/reply/error counters; the same values export to OTLP as `fusion_team_mailbox_depth`, `fusion_team_oldest_unacked_age_seconds`, `fusion_team_mailbox_consumed_total`, `fusion_team_mailbox_replies_total`.

```bash
# register a resident consumer bound to a workflow
fusion-cowork team register reviewer --workflow wf-review --team qa

# send a message (local; wakes the pool immediately)
fusion-cowork team send --from alice --to reviewer "review PR 42"

# inspect
fusion-cowork team roster
fusion-cowork team inbox reviewer
fusion-cowork team status        # requires the web daemon on :11461

# pool lifecycle (daemon-side)
fusion-cowork team start
fusion-cowork team stop
```

Shared mailbox with fusion-agent-studio:

```bash
export FUSION_TEAM_DB=~/.fusion-team/team.db   # both daemons read the same file
```

Full walkthrough, wire contract and failure semantics: `docs/team-messaging.md`. Tests: `tests/test_issue_190_mailbox_consumers.py`.

---

## 🔧 Node Reference

### macOS System Nodes

| Node | Description |
|------|-------------|
| `desktop_clean` | Organize desktop files by type/date |
| `download_organizer` | Archive, deduplicate, and clean Downloads |
| `disk_cleaner` | Scan and clean cache, temp files, `.DS_Store` |
| `file_watcher` | Watch directory for changes, trigger actions |
| `screen_capture` 🆕 | Take desktop screenshot (full/selection/window) |
| `clipboard` 🆕 | Read/write/clear system clipboard |
| `notification` 🆕 | Send macOS Notification Center alerts |
| `app_lifecycle` 🆕 | Launch/quit/activate/list macOS applications |
| `ocr` 🆕 | Recognize text from images (Vision + MLX) |

### AI Processing Nodes (`→ fusion-mlx`)

| Node | Function | Backend |
|------|----------|---------|
| `ai_classify` | Semantic file classification | `fusion-mlx /v1/chat/completions` |
| `ai_summarize` | Document summarization & report | `fusion-mlx /v1/chat/completions` |
| `ai_generate_name` | Intelligent file naming | `fusion-mlx /v1/chat/completions` |

### Tool Nodes (from Squish built-in tools)

| Node | Description |
|------|-------------|
| `shell_exec` | Execute shell commands, capture output |
| `python_repl` | Execute Python code in isolated subprocess |
| `web_search` | Search web via DuckDuckGo Lite |
| `fetch_url` | Fetch URL content, auto-extract text |
| `apply_edit` | Find-and-replace edits on files |

### IO / Logic Nodes

| Node | Description |
|------|-------------|
| `file_input` | Read file list or directory contents |
| `file_output` | Write workflow results to files |
| `xlsx_write` | Generate Excel workbooks from a JSON spec — multiple sheets, `="..."` formula cells (recompute on open), header styling, conditional formatting (issue #151; needs `pip install -e ".[office]"`) |
| `pptx_write` | Generate editable PowerPoint decks from a JSON spec — title/content/chart/table layouts, `period_compare` auto-flags >10% movements (issue #152; needs `pip install -e ".[office]"`) |
| `filter` | Filter data by conditions (extension, size, date) |
| `loop` | Batch process each item in a list |
| `merge` | Merge data from multiple upstream nodes |

### Chrome CDP Nodes 🆕 (M3)

| Node | Description |
|------|-------------|
| `cdp_navigate` | Navigate Chrome to URL via CDP |
| `cdp_snapshot` | Get page accessibility tree |
| `cdp_click` | Click element by backendNodeId |
| `cdp_fill` | Fill form field by CSS selector |
| `cdp_fill_form` | Batch fill multiple form fields |
| `cdp_screenshot` | Capture page screenshot (PNG) |
| `cdp_evaluate` | Execute JavaScript in page |
| `cdp_emulate` | Emulate device viewport |
| `cdp_network` | Query network requests |
| `cdp_console` | Query console messages |

### Plugin System 🆕 (M3)

Plugin system allows extending Fusion-Cowork with custom nodes:

```python
# Plugin manifest (manifest.json)
{
    "name": "my_plugin",
    "version": "1.0.0",
    "description": "Custom nodes",
    "nodes": ["my_custom_node"],
    "entry_point": "plugin",
    "sandbox": true,
}
```

```bash
# Install from directory or zip
fusion-cowork plugin install /path/to/plugin_dir
fusion-cowork plugin install /path/to/plugin.zip
```

**Plugin sandbox runtime isolation (v0.2.15, P1-6):** When `manifest.sandbox == true`, plugin code never runs in the main process. `PluginLoader._load_sandboxed()` spawns a `sandbox_runner` subprocess to introspect node metadata, registers `SandboxedNode` wrapper subclasses in-process, and delegates every `execute()` back to the subprocess (rlimit CPU/memory/NPROC/FSIZE + timeout). The main process never calls `importlib.exec_module` on the plugin. `sandbox == false` keeps the legacy in-process load path (backward compatible).

### Skill Mechanism 🆕 (M3)

Skills are high-level shortcuts that map to existing nodes:

| Skill | Alias | Node |
|-------|-------|------|
| `/cleanup` | 清理桌面 | `desktop_clean` |
| `/classify` | AI分类 | `ai_classify` |
| `/screenshot` | 截图 | `screen_capture` |
| `/search` | 搜索 | `web_search` |
| `/organize` | 下载整理 | `download_organizer` |
| `/diskclean` | 磁盘清理 | `disk_cleaner` |

MCP tools `skill_list` and `skill_run` are available for Claude Desktop/Code integration.

### Computer Use Nodes 🆕 (M4)

Mouse/keyboard control + AI loop for autonomous desktop operation:

| Node | Description |
|------|-------------|
| `mouse_move` | Move mouse to (x, y) coordinates |
| `mouse_click` | Click (left/right/double) at position |
| `keyboard_type` | Type text via keyboard |
| `keyboard_shortcut` | Press key combo (Cmd+C, etc.) |
| `computer_use_loop` | Screenshot → AI analyze → act → repeat loop |

```bash
# Move mouse
fusion-cowork computer-use move 500 300

# Click
fusion-cowork computer-use click --x 500 --y 300 --button left

# Type text
fusion-cowork computer-use type "Hello World"

# Keyboard shortcut
fusion-cowork computer-use shortcut c --modifiers cmd

# AI-powered Computer Use loop
fusion-cowork computer-use run "打开 Safari 并搜索天气" --max-steps 10
```

### Remote Control 🆕 (M4)

WebSocket-based remote control for external clients:

```bash
# Start remote server
fusion-cowork remote serve --port 11439 --token mytoken

# Connect from another machine
fusion-cowork remote connect ws://host:11439/control --token mytoken

# Submit workflow remotely
fusion-cowork remote submit workflow.json --url ws://host:11439/control
```

### Structured Output 🆕 (M4)

JSON Schema validation for node outputs:

```bash
# Validate data against schema
fusion-cowork schema validate data.json schema.json

# Check node output schema
fusion-cowork schema check mouse_move
```

NodeResult now supports `schema` field and `validate()` method for automatic output validation.

---

## 📝 Workflow Examples

### Desktop Daily Cleanup (JSON)

```json
{
  "name": "Desktop Daily Cleanup",
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

### AI-Generated Workflow

```bash
fusion-cowork ai generate "Every night at 9pm, clean my Downloads folder and back it up to Documents"
```

---

## 🧪 Running Tests

```bash
# Install test dependencies (same recipe as CI)
pip install -e ".[test,cloud,web,office]"
# fusion-harness kernel — engine shims need it (issue #198, PyPI since 0.2.1)
pip install fusion-harness
# (monorepo alternative: pip install -e ".[plugins]")

# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=fusion_cowork --cov-report=html
```

---

## 🛣️ Roadmap

### V0.1 (MVP) ✅
- [x] Core file automation (Desktop, Downloads, Documents)
- [x] AI batch processing (classify, summarize, rename)
- [x] Template center (10 built-in templates)
- [x] Natural language → workflow generation
- [x] Ecosystem integration (fusion-mlx, Fusion-KB)
- [x] Type coercion for LLM-generated params (from Squish)
- [x] Lazy import architecture (from Squish)
- [x] Tool nodes: Shell, Python REPL, Web Search, Fetch URL, Apply Edit

### V0.2 ✅
- [x] Enhanced scheduler (Cron UI, calendar view, task dependency, stats)
- [x] AI workflow optimizer (bottleneck detection, auto-fix, fusion-mlx suggestions)
- [x] Batch report generator (Markdown/HTML, multi-workflow summary)
- [x] macOS native .app packaging script
- [x] Screen capture (desktop screenshot with AI analysis)
- [x] Clipboard integration (read/write/clear)
- [x] System notifications (macOS Notification Center)
- [x] macOS app lifecycle (launch/quit/activate/list)
- [x] OCR text recognition (Vision + fusion-mlx)
- [x] MCP server mode (16 tools for Claude Desktop/Code)

### V0.3 ✅
- [x] Industry-specific templates (design/dev/data/ops — 10 templates)
- [x] Multi-agent orchestration (agent registration, task decomposition, parallel execution)
- [x] Cross-device collaboration (WebSocket sync, device discovery, workflow sharing)
- [x] Permission model (MANUAL/AUTO/PLAN/BYPASS — 4-tier, high-risk node approval)
- [x] Hook system (14 event types — pre/post node, workflow lifecycle, permission intercept, session, pre-compact)
- [x] Session persistence (SQLite — save/resume/fork workflows, auto-cleanup)
- [x] Streaming events (EventEmitter — pub/sub, SSE push, buffer replay)
- [x] MCP stdio transport (JSON-RPC 2.0 over stdin/stdout for Claude Code)
- [x] Desk RPC IPC (JSON-RPC 2.0 over Unix Domain Socket for Fusion-Studio)
- [x] Agent real executors (Node/Workflow/MLX/Shell — replace simulated execution)

### V0.4 ✅
- [x] Benchmark module (CapabilityMatrix — 32 capabilities, parity scoring, category breakdown)
- [x] BenchmarkRunner (node/workflow timing, warmup+repeats, summary stats)
- [x] ReportRenderer (Markdown/HTML/JSON comparison reports — Cowork vs Desk)
- [x] CLI benchmark commands (`fusion-cowork benchmark report/run`)
- [x] E2E integration tests (MCP full chain, DeskRPC full chain, Workflow+Permission+Hook+Session+Event)
- [x] DeskRPC event/session/permission handlers (9 new methods)
- [x] CLI permission commands (`fusion-cowork permission level/approve/deny/list`)

### V0.5 ✅
- [x] Computer Use nodes (MouseMove, MouseClick, KeyboardType, KeyboardShortcut, ComputerUseLoop)
- [x] pyobjc + AppleScript dual-backend for mouse/keyboard control
- [x] AI-powered Computer Use loop (screenshot → analyze → act → repeat)
- [x] Remote control server/client (WebSocket, auth token, workflow submission)
- [x] Structured output schema (OutputSchema + NodeResult.validate)
- [x] CLI commands: computer-use move/click/type/shortcut/run, remote serve/connect/submit, schema validate/check

### V0.6 ✅ (M5 — Execution Strengthening)
- [x] AgentRuntime — independent execution environment per agent with message loop & inbox queue
- [x] CoordinatorExecutor — dispatches subtasks to executor agents via orchestrator
- [x] AgentMessageBus integration — pub/sub inter-agent communication
- [x] Hook lifecycle integration — HookManager priority system, new events (SESSION_START, SESSION_END, PRE_COMPACT)
- [x] Hook-driven permission — auto-approve/deny via PERMISSION_REQUEST hook context
- [x] PermissionManager async check — supports hook interception before rule matching
- [x] FusionCoworkSDK — async HTTP client with local fallback for programmatic access
- [x] HeadlessRunner — no-CLI/no-GUI workflow execution engine
- [x] SDK lazy imports — FusionCoworkSDK, HeadlessRunner accessible via fusion_cowork package

### V0.7 ✅ (M6 — Collaboration Space)
- [x] Space models & enums — Space, SpaceMember, SpaceMessage, SpaceSnapshot, SpaceConfig, SpaceRole/SpaceStatus
- [x] SpaceStore — aiosqlite + SQLite WAL, 9 tables (spaces, members, messages, comments, agents, snapshots, invites, workflows, artifacts, sync_events)
- [x] SpaceService — CRUD + archive/unarchive + get_or_create + permission-gated operations
- [x] SpacePermission — 4-tier role matrix (Owner/Admin/Member/Viewer) + permission check + role query
- [x] SpaceMemberService — invite/join/leave/update_role/remove, invite code with expiry & max-uses
- [x] CLI space commands — `fusion-cowork space create/list/get/archive`, `fusion-cowork space member list/invite/join/remove`
- [x] DeskRPC space handlers — 11 JSON-RPC methods (desk.space.create/list/get/update/archive/delete, desk.space.member.invite/join/list/remove/update_role)
- [x] 58 tests covering models, store, service, permission, member service

### V0.7.1 ✅ (M7 — Shared Conversation + KB Binding)
- [x] SpaceChatService — shared context + Agent reply + streaming (SSE)
- [x] SpaceKBService — fusion-kb binding + document management + RAG search/query
- [x] SpaceAPI — 25 REST endpoints + SSE event stream (`/spaces/{id}/stream`)
- [x] FusionMLXClient enhancements — port fix (11432 via gateway) + retry on transient errors + stream robustness
- [x] FusionMLXClient fusion-core 基础设施采纳 (路径B, issue #58) — 借 `fusion_core.http_client.with_retry`+连接池 (统一重试+指标上报+LRU池), 保自建客户端业务层, 18 调用方零改; 成功路径空 content D-H3 守卫 (`finish_reason=empty_content`); CI 无 fusion-core 时 fallback 手写重试
- [x] KBClient completion — create_kb/delete_kb/upload_file/list_documents
- [x] SharedContext — workflow node access to space messages + KB search/query
- [x] CLI extensions — `space chat` interactive dialog + `space knowledge bind/status/upload/search/unbind`
- [x] DeskRPC handlers — 9 new methods (desk.space.chat.send/list/context, desk.space.knowledge.bind/status/upload/search/query/unbind)
- [x] 52 M7-specific tests (461 total)

### V0.8.0 ✅ (M8 — Agent Orchestration + Multi-Agent Relay)
- [x] SpaceAgentRuntime — space-level Agent runtime with isolation (add/remove/list/get/call/chain/register)
- [x] AgentStudioClient — HTTP client for Agent Studio API with retry + import-to-space
- [x] Chat relay — SpaceChatService.relay_agents() sequential multi-Agent chain
- [x] SpacePermission — `call_agent` action (owner/admin/member: ✅, viewer: ❌)
- [x] SpaceAPI — 6 new agent endpoints (GET/POST /agents, GET/DELETE /agents/{id}, POST /agents/call, POST /agents/relay)
- [x] Orchestrator bridge — register_to_orchestrator() maps space agents to Agent dataclasses
- [x] CLI — `space agent list/add/remove/call/relay` commands
- [x] DeskRPC — 5 new JSON-RPC methods (desk.space.agent.list/add/remove/call/relay)
- [x] 35 M8-specific tests (490 total)
- [x] KBClient port fix (11432→11436, issue #6)
- [x] desk.project.syncKnowledge — external KB file sync (issue #7)
- [x] desk.project.importSnapshot — session snapshot import (issue #8)
- [x] desk.project.exportToProject — export space content to fusion-projects (issue #9)

### V0.8.1 ✅ (Issue #3/#4 — Artifact Permissions + FSB Integration)
- [x] SpaceArtifactService — Artifact CRUD + ownership tracking + permission checks (issue #3)
- [x] Permission matrix — 15 actions (4 new: view/edit/share/transfer_artifact)
- [x] DeskRPC — 7 new desk.space.artifact.* handlers
- [x] ModuleRegistry — sidebar module registration (register/list/enable/disable) (issue #4)
- [x] NotificationService — approval task notification push (SSE + desk.notification.push) (issue #4)
- [x] DeskRPC — 4 desk.module.* + 3 desk.notification.* handlers
- [x] Store migration — space_artifacts columns + 2 new tables (sidebar_modules, space_notifications)
- [x] 29 new tests (18 artifact + 11 FSB, 519 total)

### Patch Releases

#### V0.3.1 — 架构债务审计修复 (10 CRITICAL A + 8 HIGH R + 15 E 工程缺陷全关闭)
架构审计 (`audit/fusion-cowork-audit-report-0825.md`, 判定 NOT PRODUCTION-READY, 10 放行门槛) 全修复, v0.3.0→v0.3.1。7 阶段独立验证 (每阶段 ruff 0 + pytest 绿 + checkpoint commit)。

- [x] **Stage 1 — 沙箱+权限+REPL+CDP/URL 安全纵深 (A 系列)**: scoped_folder 路径规范化统一 (resolve(strict=False)+expanduser); seatbelt profile 收紧; PythonREPL AST 拒危险调用深化; CDP JS eval allow_js 网关 + 表达式注入阻断; FetchURL SSRF 私网阻断 + redirect 收紧
- [x] **Stage 2 — 认证/授权四路绕过全堵 (R 系列)**: UDS 身份绑定 + 跨 handler principal 一致性; space IDOR 守卫全覆盖
- [x] **Stage 3 — 死配置接线 + MCP 映射/错误**: 死配置/未接线 feature 接通; MCP tools/call 映射校正; 错误信息脱敏
- [x] **Stage 4 — 并发正确性 (DB+类级锁+Hook)**: aiosqlite 写锁; NodeRegistry 类级 RLock 防 TOCTOU; HookManager 异步派发不阻塞事件循环
- [x] **Stage 5 — 资源管控 (泄漏+超时+僵尸)**: 子进程组 killpg 杜绝孤儿; executor wait_for 超时; 资源句柄 finally 清理
- [x] **Stage 6 — 持久化原子性+剩余工程缺陷**: E-1 trainer_node PATH 解析; E-2 AppleScript 转义; E-4 持久化原子写 (tmp+os.replace+fsync, enhanced_scheduler + ApplyEdit); E-7 DeskRPC engine 单例; E-15 schema 类级缓存 (免 47× 实例构造); R-4 断点续跑排除 running; R-5 调度失败计数熔断; R-5b cron 描述安全 int; R-6 WS 广播并发+慢客户端隔离+presence TTL GC; R-7 workflow_sync 旧版本拒收; R-8 MCP transport 有界读 8MiB
- [x] **Stage 7 — 版本 bump + 文档**: v0.3.0→v0.3.1 (pyproject.toml + `__init__.py`); README + CLAUDE.md 更新
- [x] 测试: 910 passed / 1 skipped, ruff 0 issues

#### V0.3.0 — 安全加固 (审计 74 项发现全修复)
对抗性审计 (`audit/fusion-cowork-0824.md`, 74 项: 23 CRITICAL / 20 HIGH / 18 MEDIUM / 13 LOW, 判定 NOT PRODUCTION-READY) 全修复, v0.2.15→v0.3.0。

- [x] **Stage 1 — 认证基线**: UDS socket 文件权限 0o600 + 可选 `desk.auth_token` 握手; `_authenticate()` 拒收 params 内 operator_id/user_id 身份字段 (仅信连接身份); MCP HTTP/streamable Bearer 中间件 (`mcp.auth_token`); remote TLS fail-closed (坏证书不降明文)
- [x] **Stage 2 — 输入净化**: ShellExec 改 `create_subprocess_exec(*shlex.split)` 消 shell 注入; nl_parser 逐节点校验 name ∈ NodeRegistry; PythonREPL 改 AST walk 拒危险调用; store update_space/update_member 列白名单; CDP navigate scheme 校验 (仅 http/https) + evaluate 进 HIGH_RISK; AppleScript 转义顺序修正
- [x] **Stage 3 — 权限模型 + 沙箱**: 新增 `PermissionLevel.CONFIRM` 默认 (check 重排: approve→allow / deny→deny / high-risk→deny / else→allow); HIGH_RISK_NODES 补全至 ~30 节点; ApplyEdit 加 `ensure_allowed`; register 拒覆盖内置名; sandbox=false 需 `plugins.trusted` 白名单; zip-slip 逐条 `relative_to` 校验; darwin seatbelt `sandbox-exec` 限制 fs/network + env 白名单 + setrlimit fail-closed + stdin 有界读 16MiB + traceback 不进 RPC
- [x] **Stage 4 — 并发/执行正确性**: execute() 深拷贝 nodes 避共享 BaseNode 写污染; recorder 按 execution_id 命名空间; ComputerUseLoop 动作白名单 + 高危确认; orchestrator 真保留 Task handle + cancel 真杀协程; ShellExecutor 超时 kill+wait; 各 executor `wait_for(timeout)` + finally 清理; 委托深度上限 (默认 5)
- [x] **Stage 5 — 错误处理/信息泄漏**: 错误帧带 trace_id, 栈仅日志不泄客户端; space/KB 输入校验 (必填/类型/长度); rpc_bridge params schema 校验 -32602
- [x] **Stage 6 — MEDIUM+LOW 收尾**: MessageBus Queue(1024)+deque(1000) drop; ConfigCenter 原子写 (tempfile+flock+0o600+replace) + RLock + 单例锁 + 观察者迭代快照; 插件 URL https-only + 重定向拒 + 50MiB + sha256; claude_desk command 校验; scoped_folder 单例锁 + symlink 拒; mcp_gateway spawn fail-closed (`FUSION_ENABLE_GATEWAY=1`); collab_ws auth_token + 成员校验; latent — desk.space.workflow.* 改走 SpaceArtifactService + Workflow.from_dict (原 AttributeError); JSON-RPC batch -32600 拒 + UDS 有界读 + uvicorn 并发/请求体上限
- [x] 测试: 868 passed / 1 skipped, ruff 0 issues

#### V0.2.15 (Patch) — 审计缺口补齐 Stage-1 + Stage-2 (文件沙箱 + 断点续跑 + 插件进程外隔离 + P2 测试补齐)
- [x] **P0-2 授权工作文件夹沙箱** — `security/scoped_folder.py` `ScopedFolderManager.ensure_allowed()` (resolve + relative_to 边界检查) 注入 6 个文件节点 (FileInput/Output/Copy/Move/Find/Delete + BatchRename) + ShellExec; 越界读/写拒绝或跳过
- [x] **P1-2 断点续跑** — `WorkflowEngine.execute(resume_steps=...)`: seed 已完成步骤 output_data → 跳过重跑 (SKIPPED); CLI `workflow/template run --resume <session_id>` 经 `SessionStore.resume()` 取快照; 修复 session steps_snapshot 丢 output_data bug; 3 回归测试
- [x] **P1-6 插件沙箱进程外隔离** — `sandbox=true` 插件走 `sandbox_runner` 子进程 introspect + `SandboxedNode` 包装子类 (`make_sandboxed_node_class` 动态生成), `execute()` 回子进程运行 (rlimit CPU/内存/NPROC/FSIZE + 超时); 主进程永不 `exec_module` 插件代码; 4 测试
- [x] **P2-9 MCP Streamable HTTP 测试补齐** — `tests/test_mcp_streamable.py` 覆盖 2025-03-26 spec 全链路: initialize 建 Mcp-Session-Id / notifications/initialized 202 / 未初始化拦截 (-32002) / tools/list / tools/call / Accept: text/event-stream → SSE 流 / DELETE 终止会话 / health; 12 测试
- [x] **P2-10 远程控制 TLS + attach 修复与测试** — 修复 `remote.py::_attach_session` 误调 `store.get_session()` (SessionStore 无此方法, 永抛异常 → attach 恒失败) → 改为 `store.get()`; 新增 TLS 测试 (无证书 None / 坏证书降级 None / openssl 自签名 → SSLContext TLSv1.2+) + attach 测试 (缺 session_id / 不存在 / 命中返回快照+绑定); 6 测试
- [x] 测试: 695 passed / 1 skipped (Py3.14), ruff 0 issues

#### V0.2.14 (Patch) — 协作实时能力补齐 (Agent Loop + presence + WS 双向)
- [x] **交互式对话 Agent Loop** — 新增 `agent_loop/` 包: LLM 逐轮决策 (RUN_NODE/REPLY/ASK/DONE) → NodeRegistry.create + resolve_alias 执行节点 → 观察结果回注入消息历史 → 循环至 DONE/max_steps; `interrupt()` 叫停 + `supplement()` 中途补一句再续; `agent-loop run/chat` CLI; 12 tests
- [x] **实时光标 + 成员 presence/online_status** — 新增 `space/presence.py`: 内存 `{space:{user:PresenceState}}`, 心跳时间戳判在线/离线 (60s 超时), 光标 (x,y,target) + EventEmitter 广播; REST 端点 (heartbeat/cursor/list/remove) + `desk.space.presence.*` RPC; 10 tests
- [x] **协作层 WebSocket 双向** — 新增 `server/collab_ws.py` `CollabHub`: 按 space_id 分房间, WS 连接绑定 (space_id,user_id); 入站 chat_send/cursor_move/presence/ping/leave → 出站广播 (排除发送者); 集成 SpaceChatService 持久化 + PresenceManager; FastAPI WS 端点 `/spaces/{id}/ws` + `desk.space.collab.*` RPC 轮询桥 + `collab serve` 独立 WS 服务; 13 tests
- [x] 测试: 668 passed / 1 skipped (Py3.14), ruff 0 issues

#### V0.2.13 (Patch) — Capability Parity Stage-3 (审计缺口补齐)
- [x] **P2-5 移动推送通知** — 新增 `notification/` 包: Bark / ntfy 双 provider + 本地 osascript 降级; `PushNode` 工作流节点 + `push send/config` CLI; ConfigCenter 持久化配置
- [x] **P2-6 Deep Research (多 Agent 研究)** — 新增 `research/` 包: LLM 规划分解子问题 → DuckDuckGo Lite 并行搜索 → 抓取摘要 → LLM 合成带引用报告; 全链路降级 (无 LLM → 原问题直搜 + 原始发现罗列); `research run` CLI
- [x] **P2-7 UltraReview (多 Agent 代码审查)** — 新增 `review/` 包: 多视角 (security/correctness/style/tests) 并行 LLM 审查 → 去重 + severity 排序 → LLM 合成报告; 支持 git diff 变更集审查; 全链路降级 (无 LLM → 文件清单); `review run` CLI
- [x] **P2-3 LSP 代码智能** — 定义/引用/悬停查询 (基于 tree-sitter / ripgrep 回退); `lsp` CLI 命令组
- [x] **P2-1 Worktree 隔离执行** — `template run --worktree` 在独立 git worktree 执行工作流, 避免污染工作区
- [x] **P2-8 add-from-Claude-Desktop MCP 导入** — 解析 Claude Desktop config.json, 导入 MCP server 配置; `mcp import` CLI
- [x] **P2-10 远程控制 TLS + 命名会话 attach** — `remote serve --tls` 加密通道 + `remote attach <name>` 命名会话重连
- [x] **P2-11 技能持久化 + 用户自写 skill 包** — `skill save/load` 持久化技能定义, 支持用户自定义 skill 包文件
- [x] **P1-1 上游 (issue/PR)** — fusion-studio Chat/Cowork 首页 + 授权文件夹选择 (已提 issue+PR 给 fusion-studio, 本地落地 code)
- [x] 测试: 633 passed / 1 skipped, ruff 0 issues

#### V0.2.12 (Patch) — 服务可用性审计修复 (Studio 集成)
- [x] **节点注册缺口**: 新增 `nodes/__init__.py::import_all_nodes()` 显式导入 9 个节点模块 (触发 `@register_node`); `DeskRPCServer.start()` + `desk rpc` CLI 调用之。历史 bug: 服务端仅 cli.py 副作用导入的 macos+browser 注册, `desk.nodes.list` 仅 33/47 节点可见 → 现 47/47
- [x] **节点列表字段不全**: `desk_rpc._handle_nodes_list` 委托 `NodeRegistry.list()` 返回 7 字段 (`name/display_name/category/description/icon/default_label/params_schema`); 原 3 字段取自 `__doc__`, Studio 无法渲染参数表单
- [x] **HTTP 通道默认未启**: `desk rpc` CLI 新增 `--http-port` (默认 11438), 默认并发启动 HTTP (`/rpc /health /mcp /sse`), 承载 Studio PluginBridge `plugins/*` 集成面板; `--http-port 0` 可禁用
- [x] **HTTP 版本漂移**: `mcp_http.py` `SERVER_VERSION` 硬编码 "0.1.0" → 动态读取 `__version__` (`/health` 返回真实版本)
- [x] **start.sh venv 指向错误**: 硬编码 `${PROJ_DIR}/.venv` (stale editable install `fusion_cowork-0.1.3`, 缺 plugins) → 优先 monorepo 根 `.venv` (`/Users/dahai/fusion/.venv`, 含 cowork 0.2.12 + plugins 0.3.3), 仅独立部署时回退本地。修复 `/rpc plugins.*` 一律 -32603
- [x] **doctor 健康检查增强**: 新增 HTTP 11438/health 探活 + fusion-plugins-ecosystem 可用性检查 (缺则明确提示安装命令)
- [x] 测试: 全套 593 passed (Py3.14), ruff 0 issues; UDS `desk.nodes.list` 实测 47 节点/7 字段, HTTP `/rpc plugins.ping` 实测 `{pong:true}`

#### V0.2.11 (Patch) — /rpc 端点托管 + plugins/* 委托 (#48)
- [x] 新增 `server/rpc_bridge.py` — 托管 `/rpc` JSON-RPC 端点, 委托 15 个 `plugins/*` 方法给 fusion-plugins-ecosystem `MCPHandler`
- [x] `mcp_http.py` 新增 `/rpc` 路由 (POST), 仅路由 `plugins/*`, 其余返回 -32601
- [x] `DeskRuntime` 注入 `NodeRegistry` + `TaskScheduler` + `FusionMLXClient`, Studio 可发现 cowork 节点
- [x] 依赖缺失降级: `fusion-plugins-ecosystem` 未安装时 `/rpc` 返回 -32603 + 安装提示 (新增 `[plugins]` extra)
- [x] Bug fix: `desk_rpc._handle_health` 硬编码 `version: "0.3.0"` → 动态读取 `__version__` (版本漂移修复)
- [x] Bug fix: `mcp_http.py` 移除 `from __future__ import annotations` — FastAPI 0.139 下 `Request` 注解字符串化导致路由 422
- [x] Bug fix: `mcp_http.py` `MCPToolRegistry` 改运行时导入 — Py3.11 下 `TYPE_CHECKING` 注解立即求值触发 `NameError` (Py3.14 注解延迟默认故本地未现, CI 3.11 暴露)
- [x] 降级语义: `/rpc` 非 plugins 方法 → -32601 (与依赖无关); plugins 方法依赖缺失 → -32603
- [x] 测试: 14 个 `/rpc` + `rpc_bridge` 用例, `skipif` 降级保护 CI; Py3.11 588 passed/5 skipped, Py3.14 593 passed, ruff 0 issues

#### V0.2.9 (Patch) — 商用问题修复
- [x] P0: Computer Use 循环截图以 `image_url` 多模态格式传入模型 (原盲调用, 致盲修复)
- [x] P0: `ScreenCaptureNode` 视觉分析复用同一多模态通道
- [x] P1: `_applescript_move` 缺 cliclick 时明确告警 (替代静默失败)
- [x] P1: CDP `list_network_requests`/`list_console_messages` 落地后台 reader loop 真实事件缓冲 (原 `return []` 桩)
- [x] P2: 文档计数漂移修正 (节点 33→47 / 6→7 分类, MCP 15→16, Hook 11→14, SpaceAPI 18→25)
- [x] 测试: 2 个 P0 多模态回归用例, 全套 579 passed, ruff 0 issues

#### V0.2.10 (Patch) — 鉴权对接文档修正 (#46)
- [x] 修正 `FUSION_MLX_API_KEY` 说明: 应为 fusion-gateway client key, 非 mlx backend key (两套独立鉴权)
- [x] README/README_CN 新增双鉴权说明 + 401 排错指引
- [x] `FusionMLXClient.chat()` 401/403 错误增加可操作提示 (引导检查 key 来源)
- [x] 测试: 579 passed, ruff 0 issues

#### V0.1.9 (Patch)
- [x] Remove local-only docs from git tracking (6 files → .gitignore)
- [x] feat: ast_diff module migrated from fusion-multi-node (#26, #27)

#### V0.1.8 (Patch)
- [x] Port standardization: 9760→11437, 9761→11438, 9762→11439 (#24, #25)
- [x] Ruff lint: 4 unused variable fixes in desk_rpc handlers

#### V0.1.7 (Patch)
- [x] 19 missing RPC handlers for Studio GUI (#19, #23)
- [x] P0: desk.space.chat.stream — streaming chat
- [x] P1: agent.update, snapshot CRUD (5), comment CRUD (2)
- [x] P2: workflow (3), discovery.scan, desktop (2), session snapshot (5)
- [x] Store: update_agent, remove_agent, snapshot get/delete, comment CRUD
- [x] Total RPC methods: 79 → 99

#### V0.1.6 (Patch)
- [x] RPC alias: `desk.space.chat.history` → chat.list (Studio compat, #20)
- [x] RPC alias: `desk.space.notification.*` → notification.* (Studio compat, #21)
- [x] GUI gap audit: 19 missing RPC handlers documented (#19)

#### V0.1.5 (Patch)
- [x] start.sh lifecycle manager (start/stop/restart/status/log/doctor/clean)
- [x] `python -m fusion_cowork` now works (__main__.py)
- [x] Remove desk.mlx.start/stop — product layer should not manage infra (#16)
- [x] Fix pyproject.toml description (#16)

#### V0.1.4 (Patch)
- [x] CI: GitHub Actions workflow (ruff lint + pytest + coverage)
- [x] Lint: ruff configured, F-series bugs fixed, 0 issues remaining
- [x] Test: 519 tests passing
- [x] Docs: environment variables section, CI badge

### V0.8 (Planned)
- [ ] Visual workflow editor (Fusion-Studio GUI)
- [ ] Plugin system (3rd-party node packages)
- [ ] Cloud backup & restore (optional, encrypted)
- [ ] Mobile companion app (remote trigger via WebSocket)

---

## 🔒 Security & Privacy

- **100% Local & Offline** — All operations run locally, zero file uploads
- **No Telemetry** — No network requests, no data reporting, no analytics
- **Preview Mode** — All operations support `--dry-run` preview
- **Undo Support** — Deletions default to Trash (not permanent)
- **Full Audit Trail** — Complete execution logs and review reports

---

## 🤝 Contributing

Contributions are welcome! Please read our [contributing guidelines](CONTRIBUTING.md) first.

---

## 📄 License

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

---

## Architecture Influences

Fusion-Cowork builds upon patterns from the open-source ecosystem:

| Pattern | Source | Integration |
|---------|--------|-------------|
| Tool Registry + type coercion | [Squish](https://github.com/nicepkg/squish) `tool_registry.py` | `engine/node.py` — `_coerce_int/bool/number/array` |
| Lazy import via `__getattr__` | [Squish](https://github.com/nicepkg/squish) `__init__.py` | `fusion_cowork/__init__.py` — `_LAZY_IMPORTS` |
| Tool name alias mapping | [Squish](https://github.com/nicepkg/squish) `tool_name_map.py` | `NODE_NAME_ALIASES` + `register_alias` |
| Built-in tool set | [Squish](https://github.com/nicepkg/squish) `builtin_tools.py` | 5 tool nodes: Shell, Python, Web Search, Fetch, Edit |
| Workflow engine (DAG) | [n8n](https://github.com/n8n-io/n8n) | `engine/workflow.py` — topological sort, data passing |
| MCP protocol | [LibreChat](https://github.com/danny-avila/LibreChat) | Planned for V0.2 |

---

<p align="center">
  <strong>Fusion-Cowork — Let your Mac do the work, locally and privately.</strong>
</p>
<p align="center">
  <sub>Built with ❤️ by the Fusion-MLX Team</sub>
</p>
