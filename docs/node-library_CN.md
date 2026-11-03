# 官方节点库 (issue #262)

面向客户流程的五个一方通用节点家族。所有配置均为流程实例级数据 — 节点代码零业务常量。令牌只经环境变量传递，绝不进入流程定义或日志。

节点位于 `fusion_cowork/nodes/ops/ops_nodes.py`（category `tool`）。import 时自注册；`import_all_nodes()` 加载后自动出现在 Web 节点面板与 `/api/nodes`。

贡献契约：继承 `BaseNode`，`@register_node` 装饰，声明 `get_params_schema()`（JSON Schema，画布检查器据此渲染表单），返回结构化 `NodeResult`，记录日志。安全敏感节点必须加入 `HIGH_RISK_NODES`（`fusion_cowork/engine/permission_manager.py`），从而接入平台审批闸门。

## agent_exec

以 argv（绝不走 shell）启动外部无人值守 agent/CLI 进程，采集退出码与输出尾部。

| 参数 | 类型 | 说明 |
|---|---|---|
| `command` | string[] | argv 列表；必填 |
| `cwd` | string | 必须存在 |
| `timeout` | number | 默认 120s；超时 kill |
| `env_keys` | string[] | 转发给子进程的环境变量名白名单 |
| `max_output` | number | 保留输出尾部，默认 20000 字符 |

输出：`exit_code`、`output`。信号崩溃以结构化消息失败 — 重启策略即重跑节点；持久状态交由外部存储承接（进程生命周期托管在 agent 执行面）。

## github_api

GitHub REST API 的 Issue/PR/标签操作。`GITHUB_TOKEN` 环境变量是唯一凭证通道。

| 参数 | 类型 | 说明 |
|---|---|---|
| `op` | enum | `issue_create` / `issue_comment` / `issue_set_labels` / `pr_get` / `list_labels` |
| `repo` | string | `org/repo`，必须在 `allowed_repos` 内 |
| `allowed_repos` | string[] | 白名单；白名单外大声拒绝 |
| `issue_number` | number | comment/labels/pr_get 用 |
| `title` / `body` / `labels` | — | 按 op 填充 |

`FUSION_GITHUB_API` 覆盖 API 基址（GHES / 测试）。

## command_evidence

白名单命令执行 + 证据归档。

- `allowlist`（string[]，fnmatch，同时匹配绝对路径与 basename 两种拼接形式）— 空白名单或不匹配大声拒绝，绝不静默放行。
- stdout+stderr 经脱敏（内置模式：`ghp_*`、`sk-*`、`AKIA…`、`Bearer …`、`password=…`，加自定义 `redact` 正则）写入 `evidence_dir/<ts>-<argv0>.txt`；命令行本身同样脱敏。
- `ports` + `port_registry`（JSON `{port: 期望服务是否在}`）产出对账报告（`expected` vs 实时 TCP 探测）；不一致记 WARNING。

## http_webhook

调用外部 HTTP API，带重试与出站 HMAC 签名。

| 参数 | 类型 | 说明 |
|---|---|---|
| `url` | string | 仅 http/https |
| `method` | enum | GET/POST/PUT |
| `headers` | object | 值支持 `${ENV_NAME}` 运行时插值 |
| `retry` / `retry_backoff` | number | 仅对网络错误与 5xx 重试（4xx 立即失败） |
| `sign_secret_env` | string | 存放 HMAC 密钥的环境变量名（默认 `FUSION_COWORK_WEBHOOK_SECRET`）；设置后请求携带 `X-Fusion-Signature: sha256=<hex>`（对原始 body） |

**入站回调 → 恢复被闸的流程：** `approval_gate` 节点配合签名回调路由

```
POST /api/hooks/webhook/{run_id}
X-Fusion-Signature: sha256=<hmac-sha256(body, FUSION_COWORK_WEBHOOK_SECRET) 十六进制>
{"approved": true, "params": {"k": "v"}}
```

路由校验 HMAC（`/api/hooks/*` 豁免静态 API key 鉴权 — 签名即鉴权），找到 run 的待审批项，按决定与可选编辑参数恢复。无待审批项时返回 `{"resumed": false}`。

## approval_gate

暂停流程等待人工决定。节点注册在 `HIGH_RISK_NODES`，权限层默认拒绝，run 停在 HITL 闸门 — 时间线立即出现 `blocked` 步骤（issue #260），审批收件箱带倒计时。决定经 Web 审批卡（批准一次 / 永久允许 / 拒绝，可编辑参数）或上述签名 webhook 路由送达。批准后节点原样透传输入并返回 `approved: true`。
