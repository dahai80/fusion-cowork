---
doc_id: fusion-cowork/architecture
product: fusion-cowork
class: platform
entry: service
doc_type: architecture
title: {zh: "架构说明", en: "Architecture"}
audience: engineer
status: draft
version: 0.5.85
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - fusion_cowork/engine/
  - fusion_cowork/nodes/__init__.py
  - start.sh
  - README.md
---

# 架构 / Architecture

## 分层 / Layers

```
CLI (click)  ─┐
Web API :11461 ──┼──> WorkflowEngine ──> NodeRegistry (81 types, 15 modules)
desk RPC :11438 ┘         │
                          ├─> PermissionEngine / HookEngine  (fusion-harness kernel + _*_local.py fallbacks)
                          ├─> LLMFunnel + Compactor          (fusion-harness, 9000-token compaction)
                          └─> FusionMLXClient ──> fusion-mlx :11434
```

- **入口层 / Entry**: `fusion_cowork/cli.py`（40+ 命令组）、`web_api`（REST + SSE）、`desk rpc`（UDS JSON-RPC + MCP HTTP `/mcp` `/sse`）。同一引擎，三种通道。
- **引擎层 / Engine**: `fusion_cowork/engine/workflow.py` — 拓扑序串行主循环（明确非并行）、节点间检查点（pause/resume）、运行指令（`add_directive`）、HITL 闸门（`_hitl_await`，超时自动拒绝）。
- **节点层 / Nodes**: `fusion_cowork/nodes/` 8 类（macos/ai/io/logic/tools/browser/ecosystem/ops），import 时经 `@register_node` 自注册；`import_all_nodes()` 保证服务端启动时全部加载。
- **权限层 / Permissions**: 5 级权限模型 + `HIGH_RISK_NODES` 高风险类；规则经 `PermissionManager` 持久化；审批收件箱合并本地拒绝与 guard L3。
- **内核委托 / Kernel delegation**: `engine/hooks.py`、`engine/permission.py`、`engine/funnels.py`、`engine/compactor.py` 优先路由到 fusion-harness（`KERNEL_SOURCE` 标记），同目录 `_hooks_local.py` 等保留 cowork 语义回退。
- **持久层 / Persistence**: aiosqlite 本地库；`.[cloud]` 下 Postgres + RLS + 迁移（`schema_migrations` 表）。

## Web 模块 / Web module

`web/` 是 React 18 + Vite 5 + TypeScript + Tailwind 包（`@xyflow/react` 画布）。两种部署形态：standalone（`fusion-cowork web serve` 托管 web/dist）或嵌入 fusion-web-shell（`?embedded=1`，apiBase 取 `/shell/<id>` 前缀，theme 经 SDK 订阅）。移动端只读。`server/shell_registry.py` 负责向 fusion-gateway 注册 manifest 并心跳（401 时重注册）。

## 关键流 / Key flows

1. **运行 / Run**: API/CLI → RunRegistry（异步 run_id）→ 引擎逐节点执行 → 事件经 SSE `/api/runs/{id}/events` 广播 → 产物从 step 输出派生。
2. **审批 / Approval**: 高风险节点 → 运行暂停（HITL）→ `/api/permissions/pending` 出现条目 → confirm（once / always-allow / deny）→ 按执行 ID 唤醒运行。
3. **恢复 / Recovery**: 失败 run → `POST /api/runs/{id}/resume-from {step_id}` → 基于快照的关联新 run。
4. **调度 / Schedule**: `schedule add` → TaskScheduler cron → 触发存储的工作流模板。

## 约束与取舍 / Constraints and trade-offs

- 主循环拓扑序串行：确定性高、易审计，代价是节点级并行要靠服务波次并发（默认 4）。
- 引擎依赖 fusion-harness：缺失时 import 即失败并给出安装提示（`engine/_harness_guard.py`），不存在静默降级副本。
- 安全边界在本机：文件操作限定 workspace root（防目录穿越），浏览器走托管后端一次性 profile，公网暴露需显式配置。
