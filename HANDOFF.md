# Fusion-Desk Handoff Document

## 项目状态

**版本**: V0.4 ✅ (V0.1~V0.4 全部完成)
**全量测试**: 229 passed, 0 failed, 1 skipped
**分支**: main (clean)

---

## 已完成里程碑

### M1 — MCP stdio + Desk↔Studio IPC + Agent 真实执行
- `fusion_desk/server/mcp_transport.py` — StdioTransport (JSON-RPC 2.0 over stdin/stdout)
- `fusion_desk/server/desk_rpc.py` — DeskRPCServer (JSON-RPC 2.0 over UDS, 15→24个方法)
- `fusion_desk/server/mcp_http.py` — HTTP SSE 传输 (FastAPI)
- Agent 执行器: NodeExecutor/WorkflowExecutor/MLXExecutor/ShellExecutor
- AgentMessageBus — 发布/订阅 + 点对点通信

### M2 — 权限模型 + Hook系统 + 会话持久化 + 流式输出
- `fusion_desk/engine/permission.py` — PermissionManager (4级: MANUAL/AUTO/PLAN/BYPASS)
- `fusion_desk/engine/hooks.py` — HookManager (11事件类型, HookContext.cancel/modify)
- `fusion_desk/engine/session.py` — SessionStore (SQLite, save/get/list/fork/delete/cleanup)
- `fusion_desk/engine/events.py` — EventEmitter (pub/sub, asyncio.Queue, SSE格式, buffer replay)
- WorkflowEngine 集成: permission检查、hook拦截、session自动保存、event推送
- HIGH_RISK_NODES: shell_exec, python_repl, file_delete, apply_edit, browser_automate

### M3 — MCP权限拦截 + SSE事件流 + Session集成
- MCPToolRegistry._execute_tool() — 执行前权限检查 + PRE/POST_NODE_EXECUTE hook
- MCPServer.serve_http(event_emitter) — SSE端点对接EventEmitter
- WorkflowEngine.execute() — 完整中间件链路

### M4 — DeskRPC事件/会话/权限 + CLI权限命令
- DeskRPCServer 新增9个handler: desk.events.subscribe/recent, desk.session.list/get/fork, desk.permission.check/approve/deny/list
- CLI permission group: level/approve/deny/list
- RichConsole.print_result() 方法补全

### M5 — 功能对比矩阵 + Benchmark报告 + E2E测试
- `fusion_desk/benchmark/matrix.py` — CapabilityMatrix (32能力, 9分类, 对等率1.78, Desk独有14项)
- `fusion_desk/benchmark/runner.py` — BenchmarkRunner (节点/工作流计时, warmup+repeats)
- `fusion_desk/benchmark/report.py` — ReportRenderer (Markdown/HTML/JSON)
- CLI benchmark group: report/run
- E2E测试: MCP全链路、DeskRPC全链路、Workflow+Permission+Hook+Session+Event

---

## 文件清单 (新增/修改)

### 新增文件
- `fusion_desk/engine/permission.py`
- `fusion_desk/engine/hooks.py`
- `fusion_desk/engine/session.py`
- `fusion_desk/engine/events.py`
- `fusion_desk/server/mcp_transport.py`
- `fusion_desk/server/mcp_http.py`
- `fusion_desk/server/desk_rpc.py`
- `fusion_desk/benchmark/__init__.py`
- `fusion_desk/benchmark/matrix.py`
- `fusion_desk/benchmark/runner.py`
- `fusion_desk/benchmark/report.py`
- `tests/test_m1.py` (26 tests)
- `tests/test_m2.py` (37 tests)
- `tests/test_m3.py` (14 tests)
- `tests/test_m4.py` (18 tests)
- `tests/test_m5.py` (44 tests)

### 修改文件
- `fusion_desk/engine/__init__.py` — 新增 M2/M3 导出
- `fusion_desk/engine/node.py` — NodeStatus.DENIED/CANCELLED
- `fusion_desk/engine/workflow.py` — WorkflowEngine 接受 permission/hook/session/event
- `fusion_desk/server/mcp_server.py` — MCPToolRegistry 权限/hook, MCPServer event_emitter
- `fusion_desk/cli.py` — session/permission/benchmark 命令组, RichConsole.print_result
- `README.md` — V0.3/V0.4 roadmap, CLI命令文档

---

## 关键 API 速查

### WorkflowEngine
```python
engine = WorkflowEngine(
    permission_manager=PermissionManager(level=PermissionLevel.BYPASS),
    hook_manager=HookManager(),
    session_store=SessionStore(db_path="~/.fusion-desk/sessions.db"),
    event_emitter=EventEmitter(),
)
result = await engine.execute(workflow)  # WorkflowStatus.SUCCESS/FAILED/CANCELLED
```

### MCP Tool 调用链
```
MCPToolRegistry.call_tool() → _execute_tool()
  → PermissionManager.check() → [denied] return error
  → HookManager.fire(PRE_NODE_EXECUTE) → [cancelled] return error
  → NodeRegistry.create() → node.execute()
  → HookManager.fire(POST_NODE_EXECUTE)
```

### DeskRPC 方法 (24个)
```
desk.health, desk.nodes.list/info/execute,
desk.workflow.list/create/run/status,
desk.agent.list/submit/status,
desk.mlx.status/start/stop,
desk.system.info,
desk.events.subscribe/recent,
desk.session.list/get/fork,
desk.permission.check/approve/deny/list
```

### CLI 命令
```
fusion-desk benchmark report --format markdown|html|json [-o file]
fusion-desk benchmark run --node file_input --repeats 3
fusion-desk permission level <manual|auto|plan|bypass>
fusion-desk permission approve/deny <tool_name> --scope <scope>
fusion-desk permission list
fusion-desk session list/show/fork/delete/cleanup
fusion-desk mcp serve [--transport stdio|http] [--port 9761]
fusion-desk desk rpc
```

---

## 关键数据 (vs Claude Cowork)

| 指标 | 值 |
|------|-----|
| 总能力数 | 32 |
| Desk FULL+ | 32 |
| Desk ADVANCED | 10 |
| Cowork FULL+ | 14 |
| Desk 独有 | 14 |
| Cowork 独有 | 0 |
| 对等率 | 1.78 |

### Desk 独有优势 (14项)
工作流引擎、模板中心、NL生成工作流、定时调度、Hook系统、MCP服务端、IPC RPC、SSE事件流、会话持久化、跨设备同步、嵌入式浏览器、本地大模型、完全离线、数据隐私

---

## 已知限制 / 后续方向

1. **V0.5 计划**: 可视化工作流编辑器(Fusion-Studio GUI)、插件系统、云备份(可选加密)、手机伴侣App
2. **BenchmarkRunner.run_node** 只执行一次，循环由 `run_nodes` 负责
3. **WorkflowEngine** 对 permission denied 返回 `FAILED`，hook cancel 返回 `CANCELLED`（非 SUCCESS）
4. **CLI benchmark report --format json** 输出含 INFO 日志行，解析需跳过
5. **PermissionManager** 无 db_path 构造参数，使用 `save(path=)/load(path=)` 持久化

---

## 用户约束 (verbatim)

- ~/claude-home/fusion-mlx 为底座，其他 ~/fusion 目录下 fusion-xx 各自有自己的特性
- GUI 放在 fusion-studio 项目，只改 fusion-desk 和 fusion-studio
- 其他项目有问题和需求给它们提 issue 和 pr
- 遇到上游问题，先提 issue，再提 pr，跟着提交落地 code
- source .venv/bin/activate 后再工作
- 4的倍数缩进，不生成 docstring，代码必须有日志
- 每次更新代码需要更新 README.md
