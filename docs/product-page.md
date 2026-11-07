---
doc_id: fusion-cowork/product-page
product: fusion-cowork
class: platform
entry: service
doc_type: product-page
title: {zh: "Fusion-Cowork 产品页", en: "Fusion-Cowork product page"}
audience: engineer
status: draft
version: 0.5.85
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - pyproject.toml
  - start.sh
platform:
  serves: "DAG workflow engine + macOS desktop automation: author, schedule, run and audit local automation workflows via CLI, desk RPC, MCP HTTP (:11438) and a web canvas (:11461)."
  builds_on: "fusion-harness (hooks/permissions/funnels/compaction kernel, shims in fusion_cowork/engine/), fusion-mlx (:11434, LLM inference via FusionMLXClient), fusion-core (shared Python foundation)."
  hosts: "web/ React canvas module (standalone or embedded in fusion-web-shell via the /shell module prefix), markdown skill packages, JSON capability packs, scheduled tasks, team messaging."
  integration_surface: "REST + SSE on :11461 (auth: static FUSION_COWORK_WEB_API_KEY or gateway-issued fm- key), MCP HTTP on :11438 (fusion-cowork mcp serve / desk rpc), UDS /tmp/fusion-cowork.sock, gateway shell-manifest registration (FUSION_SHELL_GATEWAY_URL)."
  extension_points: "@register_node custom node types, markdown skill packages (handler=workflow|node|script), JSON skill packs (SkillRegistry), plugin import (fusion-cowork plugin install), web manifest pages/searchProviders/notifications, ecosystem nodes (studio_run, mcp_client, fusion_trainer, memory_commit/memory_retrieve, fusion_browser)."
---

# Fusion-Cowork 产品页 / Product Page

## 产品定位 / What it is

Fusion-Cowork 是 fusion 生态的平台层产品：一个本地优先的 DAG 工作流引擎，面向 macOS Apple Silicon，把桌面自动化（文件整理、浏览器操作、文档产出、定时任务）表达为可审计、可审批、可恢复的工作流。

Fusion-Cowork is a platform-layer product of the fusion ecosystem: a local-first DAG workflow engine for macOS Apple Silicon that expresses desktop automation (file housekeeping, browser operation, document deliverables, scheduled tasks) as auditable, approval-gated, recoverable workflows.

## 平台能力 / Platform capabilities

### DAG 工作流引擎 / DAG workflow engine

消费者不再需要自己编写：执行排序（拓扑串行）、节点超时看门狗（`FUSION_COWORK_NODE_TIMEOUT_S`，默认 1800s）、心跳（`FUSION_COWORK_NODE_HEARTBEAT_S`，默认 30s）、断点恢复（`resume-from`）、运行指令（`set_params` / `insert_node` / `skip_remaining`）。

Consumers no longer write: execution ordering (topological serial), per-node watchdogs, heartbeats, checkpoint recovery (`POST /api/runs/{id}/resume-from`), or run directives.

约束 / Constraint: 主循环是拓扑序串行执行，不是并行调度；并发只发生在服务层的 ready-set 波次（`FUSION_COWORK_MAX_CONCURRENCY`，默认 4）。The main loop is topological-order serial, not a parallel scheduler; concurrency exists only at the service ready-set wave level.

### 审批与权限（HITL）/ Approvals and permissions (HITL)

消费者不再需要自己编写：高风险节点闸门（`HIGH_RISK_NODES`）、5 级权限模型、审批收件箱（本地拒绝 + guard L3 合并）、HITL 暂停（`FUSION_COWORK_HITL_PAUSE` 默认开启，等待上限 `FUSION_COWORK_HITL_TIMEOUT` 默认 1800s，超时自动拒绝）。

Consumers no longer write: high-risk node gates, the 5-level permission model, the merged approval inbox, or HITL pause/resume with timeout auto-deny.

约束 / Constraint: 权限规则默认进程内持久化；`FUSION_REQUIRE_JWT=1` 且 fusion-identity 启用时失败即拒绝（fail-closed），不回退到静态 token。Permission rules persist per process; with `FUSION_REQUIRE_JWT=1`-style prod JWT enforcement the system is fail-closed, with no static-token fallback.

### 节点库 / Node library

消费者不再需要自己编写：81 种内置节点类型（macOS 文件/输入、AI 分类摘要、浏览器 + CDP 自动化、HTTP/webhook、生态节点）的分类、注册、参数校验（coerce/validate 吸纳自 Squish ToolRegistry）。

Consumers no longer write: registration, classification or parameter coercion for 81 built-in node types across macos/ai/io/logic/tools/browser/ecosystem/ops categories.

约束 / Constraint: 节点模块必须在服务启动时显式导入（`import_all_nodes()`），否则注册表残缺；自定义节点需通过 `@register_node` 并保证模块被加载。Node modules must be imported at server startup (`import_all_nodes()`); custom nodes must use `@register_node` inside a module that is loaded.

### Web 画布 / Web canvas

消费者不再需要自己编写：工作流画布 UI（React 18 + Vite 5 + `@xyflow/react`，桌面编辑、移动只读）、运行轮询、HITL 审批卡片、union-web 嵌入适配（`?embedded=1` 隐藏自带导航，SDK 提供 theme/apiBase 桥）。

Consumers no longer write: the workflow canvas UI (desktop editing, mobile read-only), run polling, HITL approval cards, or the union-web embedded-mode adapter.

约束 / Constraint: 移动端只读（`devices: ["desktop", "mobile-readonly"]`）；`/api` 路径在配置 `FUSION_COWORK_WEB_API_KEY` 或网关签发 `fm-` key 后强制鉴权。Mobile is read-only; `/api` paths enforce auth once a static key or gateway-issued `fm-` key is configured.

### 生态集成 / Ecosystem integration

消费者不再需要自己编写：调用 fusion-agent-studio（`studio_run` 节点）、外部 MCP 服务器（`mcp_client` 节点，Streamable HTTP）、fusion-trainer（`fusion_trainer`）、fusion-memory（`memory_commit`/`memory_retrieve`）、fusion-browser 托管后端（`fusion_browser`）、fusion-gateway 模块注册与心跳（`web/manifest.json`）。

Consumers no longer write: client plumbing for agent-studio, external MCP servers, fusion-trainer, fusion-memory, fusion-browser, or the fusion-gateway shell-manifest registration and heartbeat.

约束 / Constraint: 各生态节点依赖对应服务在本机可达（默认 127.0.0.1 端口，如 agent-studio :11442，可用 `FUSION_AGENT_STUDIO_URL` 覆盖）；未运行时节点显式失败，不静默跳过。Ecosystem nodes require the peer service to be reachable; they fail visibly rather than skip silently.

## 边界 / Non-goals

不提供云端执行、不支持 Windows/Linux 桌面自动化（macOS 为主）、不做模型推理（推理委托给 fusion-mlx）。No cloud execution, no Windows/Linux desktop automation, no model inference of its own (delegated to fusion-mlx).
