---
doc_id: fusion-cowork/troubleshooting
product: fusion-cowork
class: platform
entry: service
doc_type: troubleshooting
title: {zh: "故障排查", en: "Troubleshooting"}
audience: engineer
status: draft
version: 0.5.85
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - start.sh
  - README.md
  - docs/guides/guide.md
  - docs/api-reference.md
---

# 故障排查 / Troubleshooting

## 服务起不来 / Service will not start

**`start.sh start` 报 venv 缺失** — start.sh 优先用 monorepo 根 `.venv`（`~/fusion/.venv`），独立部署才回退本目录。确认路径存在：`ls ~/fusion/.venv/bin/activate`；不存在则 `cd ~/fusion && scripts/sync-deps.sh`。

**`registry-check` 失败（exit 1）** — 端口未在 `~/fusion/architecture/port-registry.yaml` 登记。先登记再绑定；临时绕过用 `FUSION_SKIP_REGISTRY_CHECK=1`（仅限实验）。

**端口被占** — `lsof -iTCP:11438 -sTCP:LISTEN` 找占用者；cowork 家族端口：11438（desk MCP HTTP）、11439（远程 WS）、11437（同步）、11461（Web API）。

## 引擎与节点 / Engine and nodes

**节点缺失或模板反序列化失败（注册表残缺）** — 服务端必须显式导入全部节点模块（`import_all_nodes()`，15 模块 / 81 节点类型）。历史 bug：仅 CLI 副作用导入时只有 33/47 节点可见。用 `web serve` 或 `desk rpc` 启动而不是直接跑脚本；`GET /api/nodes` 核对。

**缺 fusion-harness，import 直接失败** — `pip install fusion-harness`（monorepo 内 `pip install -e ".[plugins]"`）。这是硬依赖（`engine/_harness_guard.py`），不存在回退副本。

**节点超时** — 默认单节点 1800s（`FUSION_COWORK_NODE_TIMEOUT_S`）；长任务在节点 `config.timeout` 覆盖，或调大环境变量；`0` 关闭看门狗。日志按 run 过滤：`grep "[<run_id>]" logs/…`。

## 审批与权限 / Approvals and permissions

**运行卡在 pending** — HITL 默认开启：运行暂停等待审批。`GET /api/permissions/pending` 查看待审条目，`POST /api/permissions/pending/{action_id}/confirm` 处理；无人值守场景设 `FUSION_COWORK_HITL_PAUSE=0`（deny-fast）。等待上限 `FUSION_COWORK_HITL_TIMEOUT`（默认 1800s），超时自动拒绝，产生 `COMPLETED_WITH_DENIALS` 终态（若节点 `continue_on_error=True`）。

**confirm 返回 404 / 条目消失** — 审批等待超时后引擎已丢弃 pending（`discard_pending_by_execution`）；重新提交运行。

## Web API 与鉴权 / Web API and auth

**`401 {"error":{"code":"unauthorized"}}`** — 已配置 `FUSION_COWORK_WEB_API_KEY` 或网关签发了 `fm-` 模块 key（存于 `~/.fusion-cowork/web_module_key`）。请求带 `Authorization: Bearer <key>` 或 `x-api-key: <key>`。

**网关注册失效（shell 标签 503/401）** — 心跳 401 会触发重注册；`/api/models` 返回 `unavailable: true` 表示上游模型列表冷缓存失败，前端轮询会自停，属预期降级而非故障。

## 上游依赖 / Upstream dependencies

**`ai generate` 失败** — 需要 fusion-mlx 在 :11434：`~/fusion/fusion-mlx/start.sh status`。模型过滤规则：embedding/draft 模型不会被选为 chat 默认；`FUSION_MLX_MODEL` 可显式指定。

**`studio_run` / `mcp_client` 节点失败** — 对端服务不可达时节点显式失败（不静默跳过）。核对 `FUSION_AGENT_STUDIO_URL`（默认 :11442）与 MCP 服务器地址。

## 云模式 / Cloud mode

**数据库迁移报 `no such column: tenant_id`** — 旧库升级路径由 MigrationRunner 处理租户索引；确认用的是 `serve --container` 启动且 `DATABASE_URL` 指向 Postgres。本地默认是 SQLite，不需要 DSN。

## 诊断命令 / Diagnostics

```bash
./start.sh doctor          # venv / CLI / socket / upstream health
./start.sh log 200         # last 200 log lines
./start.sh log -f          # follow
fusion-cowork system info  # runtime, nodes, budget
curl -s http://127.0.0.1:11438/health
```
