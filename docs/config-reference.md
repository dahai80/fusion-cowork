---
doc_id: fusion-cowork/config-reference
product: fusion-cowork
class: platform
entry: service
doc_type: config-reference
title: {zh: "配置参考", en: "Configuration reference"}
audience: engineer
status: draft
version: 0.5.85
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - start.sh
  - .env.example
  - fusion_cowork/cli.py
  - docs/api-reference.md
---

# 配置参考 / Configuration Reference

配置来源只有环境变量与 CLI 参数（无配置文件；`.env.example` 是模板，`cp .env.example .env` 后由 start.sh 通过 `set -a` 加载）。Configuration comes from environment variables and CLI options only; `.env.example` is the template, loaded by start.sh via `set -a`.

## 服务与端口 / Services and ports

| Env / CLI | Default | Meaning / 说明 |
|---|---|---|
| `FUSION_PORT` / `desk rpc --http-port` | 11438 | desk MCP HTTP（`/rpc` `/health` `/mcp` `/sse`），port-registry `fusion-cowork` |
| `FUSION_BIND_HOST` | 127.0.0.1 | bind host；`0.0.0.0` 需在 port-registry 登记 reason |
| `SOCK_FILE` | /tmp/fusion-cowork.sock | desk RPC UDS 路径 |
| `web serve --port` | 11461 | Web API（REST + web/dist），port-registry `fusion-cowork-web` |
| `FUSION_COWORK_WEB_URL` | http://127.0.0.1:11461 | web 模块自身地址 |
| `FUSION_REGISTRY` | ~/fusion/architecture/port-registry.yaml | start.sh 端口自检用的 registry 路径 |
| `FUSION_SKIP_REGISTRY_CHECK` | unset | 置 1 跳过 start.sh 的端口注册自检 |

## 鉴权 / Auth

| Env | Default | Meaning / 说明 |
|---|---|---|
| `FUSION_COWORK_WEB_API_KEY` | unset | 静态 web API key；未配置且无网关签发 key 时 `/api` 放行（仅本机绑定） |
| `FUSION_SHELL_GATEWAY_URL` + `FUSION_ADMIN_KEY` | unset | 同时配置后向 fusion-gateway 注册 `web/manifest.json` 并心跳保活 |
| `FUSION_IDENTITY_ENABLED` | off | 启用 fusion-identity 作为唯一 JWT 签发方/租户注册表（默认关闭） |
| `FUSION_IDENTITY_SERVICE_TOKEN` | unset | identity 服务间调用凭证 |
| `FUSION_REQUIRE_JWT` | unset | 置 1 后生产模式验证失败即拒绝（fail-closed），无静态 token 回退 |
| `FUSION_COWORK_CROSS_SYNC` | unset | 置 1 启动跨设备同步（`FUSION_COWORK_SYNC_HOST/PORT/TOKEN/CERT/KEY`，:11437） |

## 引擎行为 / Engine behavior

| Env | Default | Meaning / 说明 |
|---|---|---|
| `FUSION_COWORK_HITL_PAUSE` | on（暂停等待审批） | `0/false/off/no` → 无人值守 deny-fast |
| `FUSION_COWORK_HITL_TIMEOUT` | 1800 | 审批等待上限，支持 `s/m/h` 后缀；超时自动拒绝该节点 |
| `FUSION_COWORK_NODE_TIMEOUT_S` | 1800 | 单节点看门狗；节点可用 `config.timeout` 覆盖，`0` 关闭 |
| `FUSION_COWORK_NODE_HEARTBEAT_S` | 30 | 长节点 PROGRESS 心跳间隔 |
| `FUSION_COWORK_MAX_CONCURRENCY` | 4 | 服务 ready-set 波次并发（start.sh 默认值） |
| `FUSION_COWORK_WORKSPACE_ROOT` | 用户主目录 | 系统目录浏览（`/api/workspaces/system-dirs`）的根 |
| `FUSION_COWORK_LIFECYCLE_DIR` | unset | 生命周期事件 JSONL 落盘目录 |
| `FUSION_COWORK_TRUSTED_SYNC_URLS` | unset | 逗号分隔 URL 前缀，作为 `http_request` 允许规则种子；`off/none/false/0` 禁用 |

## 上游服务 / Upstream services

| Env | Default | Meaning / 说明 |
|---|---|---|
| `FUSION_MLX_BASE_URL`（FusionMLXClient） | http://127.0.0.1:11434 | fusion-mlx 推理地址 |
| `FUSION_MLX_MODEL` | 自动选择 | NL 生成工作流使用的 chat 模型 |
| `FUSION_AGENT_STUDIO_URL` | http://127.0.0.1:11442 | `studio_run` 节点的 agent-studio 地址 |
| `FUSION_AGENT_STUDIO_API_KEY` | unset | 以 `x-api-key` 发送给 agent-studio |
| `FUSION_RAG_URL` | http://127.0.0.1:11436 | Extensions 知识库页使用的 fusion-rag 地址 |
| `FUSION_BROWSER_CONFIG` | unset | `fusion_browser` 节点的托管后端配置 |

## 云模式 / Cloud mode

| Env | Default | Meaning / 说明 |
|---|---|---|
| `DATABASE_URL` / `FUSION_PG_DSN` | SQLite（本地）；示例见 .env.example | Postgres DSN（`.[cloud]`，`serve --container`） |
| `FUSION_JWT_SECRET` | 必填（云模式） | HS256 签名密钥，用 `openssl rand -hex 32` 生成，不要写入仓库 |

限制 / Constraints：Redis 目前无依赖（`.env.example` 预留约定路径）；所有 bind 默认 127.0.0.1，改公网绑定前先改 `architecture/port-registry.yaml`。
