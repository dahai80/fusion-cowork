# Fusion-Desk 整改方案与实施计划

> 生成日期: 2026-07-28 | 基于 claude-cowork-insight.md 对标结论 | 仅修改 fusion-desk + fusion-studio

---

## 一、整改原则

1. **只改两个项目**: fusion-desk (Python 后端) + fusion-studio (SwiftUI GUI)
2. **fusion-mlx 为底座**: 不修改，通过 HTTP API (localhost:8000) 调用
3. **其他 fusion-xx**: 有需求提 issue/PR，不直接改
4. **先通脉再强骨**: MCP→IPC→Agent→权限→会话→流式→插件→CDP
5. **每步可验证**: 每个里程碑有明确的验收标准

---

## 二、里程碑总览

| 阶段 | 里程碑 | 周期 | 核心交付 | 涉及项目 |
|------|--------|------|----------|----------|
| M1 | 脉络打通 | W1-W3 | MCP stdio 传输 + Desk IPC + Agent 真实执行 | desk + studio |
| M2 | 安全与持久 | W4-W6 | 权限模型 + 会话持久化 + 流式输出 | desk + studio |
| M3 | 生态扩展 | W7-W9 | 插件系统 + 技能机制 + Chrome CDP | desk + studio |
| M4 | 闭环增强 | W10-W12 | Computer Use + 远程控制 + 结构化输出 | desk + studio |

---

## 三、M1: 脉络打通 (W1-W3)

### 3.1 MCP stdio 传输层 (W1)

**目标**: fusion-desk 可作为 MCP server 被 Claude Desktop/Code 调用

**fusion-desk 改动**:

```
fusion_desk/server/
├── mcp_server.py        # 重构: 拆分工具注册与传输
├── mcp_transport.py     # 新增: stdio 传输 (stdin/stdout JSON-RPC)
├── mcp_http.py          # 新增: HTTP+SSE 传输 (FastAPI)
└── __init__.py
```

**mcp_transport.py 核心设计**:
- 读取 stdin 的 JSON-RPC 请求 (`initialize`, `tools/list`, `tools/call`)
- 写入 stdout 的 JSON-RPC 响应
- 支持 MCP 规范的 `notifications/tools/list_changed`
- 生命周期: `initialize` → `initialized` → 正常交互 → `shutdown`

**mcp_http.py 核心设计**:
- FastAPI 路由: `POST /mcp` (JSON-RPC), `GET /sse` (Server-Sent Events)
- SSE 用于实时推送工具调用进度
- 复用现有 `[web]` 依赖 (fastapi/uvicorn)

**CLI 集成**:
```bash
fusion-desk mcp serve --transport stdio    # Claude Code 调用
fusion-desk mcp serve --transport http --port 9761  # HTTP 模式
```

**验收标准**:
- [ ] `claude mcp add fusion-desk -- fusion-desk mcp serve --transport stdio` 成功注册
- [ ] Claude Code 可通过 MCP 调用 `take_screenshot`, `clipboard_read`, `run_workflow`
- [ ] `fusion-desk mcp serve --transport http --port 9761` 可通过 HTTP 调用

### 3.2 Desk↔Studio IPC 连通 (W2)

**目标**: fusion-studio DeskView 可调用 fusion-desk 工作流引擎

**fusion-desk 改动** — 新增 JSON-RPC 服务端:

```python
# fusion_desk/server/desk_rpc.py (新增)
class DeskRPCServer:
    """JSON-RPC 2.0 服务端 — 供 fusion-studio 通过 UDS 调用。"""

    # 监听 /tmp/fusion-desk.sock
    # 方法:
    #   desk.list_templates     → 列出工作流模板
    #   desk.get_template       → 获取模板详情
    #   desk.run_template       → 执行模板
    #   desk.run_workflow       → 执行自定义工作流
    #   desk.list_nodes         → 列出可用节点
    #   desk.get_node_schema    → 获取节点参数 schema
    #   desk.create_template    → 创建/保存模板
    #   desk.list_tasks         → 列出执行历史
    #   desk.get_task_status    → 获取任务状态
    #   desk.cancel_task        → 取消任务
    #   desk.list_schedules     → 列出定时任务
    #   desk.create_schedule    → 创建定时任务
```

**fusion-studio 改动** — DeskView 重构:

```swift
// FusionStudio/Modules/Desk/DeskView.swift 重构
// 1. 注入 IPCClient，连接 /tmp/fusion-desk.sock
// 2. 模板列表从 RPC 获取 (非硬编码 deskPresets)
// 3. 运行模板调用 desk.run_template
// 4. 任务状态实时更新 (desk.get_task_status)
// 5. 节点编辑器: 从 desk.list_nodes 获取节点 → 可视化编排
```

**IPC 协议对齐**:
- 复用 fusion-studio 已有的 IPCClient (JSON-RPC 2.0 over UDS)
- 新增命名空间 `desk.*`
- socket 路径: `/tmp/fusion-desk.sock` (与 `/tmp/fusion-studio.sock` 并行)

**验收标准**:
- [ ] fusion-studio DeskView 显示 fusion-desk 的真实模板列表
- [ ] 点击"运行"→ fusion-desk 执行工作流 → 结果回传到 UI
- [ ] 任务执行状态实时更新 (pending→running→completed/failed)

### 3.3 Agent 真实执行 (W3)

**目标**: AgentOrchestrator 从模拟执行升级为真实协程执行

**fusion-desk 改动**:

```python
# fusion_desk/orchestrator/orchestrator.py 重构
class AgentOrchestrator:
    # 1. Agent 可绑定一个 Workflow 或一个 Node
    # 2. _execute_task 真实调用 node.execute()
    # 3. Agent 间通过消息队列通信
    # 4. 支持 asyncio.Task 后台运行
```

**新增 Agent 执行器**:

```python
# fusion_desk/orchestrator/executors.py (新增)
class NodeExecutor:
    """将 Node 包装为 Agent 执行器。"""
    async def __call__(self, input_data):
        node = NodeRegistry.create(self.node_name, config=NodeConfig(params=input_data))
        result = await node.execute(input_data)
        return result.data

class WorkflowExecutor:
    """将 Workflow 包装为 Agent 执行器。"""
    async def __call__(self, input_data):
        engine = WorkflowEngine()
        result = await engine.execute(self.workflow, input_data)
        return result

class MLXExecutor:
    """将 fusion-mlx 调用包装为 Agent 执行器。"""
    async def __call__(self, input_data):
        client = FusionMLXClient()
        response = await client.chat(
            model=self.model,
            messages=[{"role": "user", "content": input_data.get("prompt", "")}],
        )
        return {"content": response.content}
```

**Agent 间通信**:

```python
# fusion_desk/orchestrator/comm.py (新增)
class AgentMessageBus:
    """Agent 间异步消息总线。"""
    # asyncio.Queue per agent
    # send(target_agent, message)
    # receive() → awaitable
```

**验收标准**:
- [ ] `orchestrator.run_standard_pipeline()` 真实执行 Node/Workflow，非模拟
- [ ] Agent 间可传递数据 (Planner 输出 → Executor 输入)
- [ ] 后台 Agent 异步执行，主流程不阻塞

---

## 四、M2: 安全与持久 (W4-W6)

### 4.1 权限模型 (W4)

**目标**: 每个工具调用可被审批/拒绝，支持分级权限

**fusion-desk 改动**:

```python
# fusion_desk/engine/permission.py (新增)
class PermissionLevel(Enum):
    MANUAL = "manual"          # 每次确认
    AUTO = "auto"              # 自动放行已批准的操作
    PLAN = "plan"              # 规划阶段放行，执行需确认
    BYPASS = "bypass"          # 全部放行 (仅限沙箱)

class Permission:
    tool_name: str
    allowed: bool
    scope: str                 # "file:~/Desktop/**" / "command:git *" 等

class PermissionManager:
    level: PermissionLevel
    rules: List[Permission]

    def check(self, tool_name: str, params: dict) -> bool:
        """检查工具调用是否被允许。"""

    def approve(self, tool_name: str, scope: str) -> None:
        """批准工具调用范围。"""
```

**Node 集成**:

```python
# BaseNode.execute() 前置权限检查
class BaseNode:
    async def execute(self, inputs):
        # 权限检查
        if self._permission_manager:
            allowed = self._permission_manager.check(self.name, inputs)
            if not allowed:
                return NodeResult(status=NodeStatus.DENIED, error="权限不足")
        # ... 原有逻辑
```

**高风险节点**: ShellExecNode, PythonREPLNode, FileDeleteNode 默认需 MANUAL 确认

**Hook 生命周期 (W4 同步)**:

```python
# fusion_desk/engine/hooks.py (新增)
class HookEvent(Enum):
    PRE_NODE_EXECUTE = "pre_node_execute"       # 节点执行前
    POST_NODE_EXECUTE = "post_node_execute"      # 节点执行后
    WORKFLOW_START = "workflow_start"             # 工作流开始
    WORKFLOW_END = "workflow_end"                 # 工作流结束
    PERMISSION_REQUEST = "permission_request"     # 权限请求
    CONFIG_CHANGE = "config_change"               # 配置变更
    AGENT_START = "agent_start"                   # Agent 启动
    AGENT_STOP = "agent_stop"                     # Agent 停止
    NOTIFICATION = "notification"                 # 通知事件

class HookManager:
    """Hook 管理器 — 对标 Claude Cowork 的 11 种 Hook。"""
    handlers: Dict[HookEvent, List[Callable]]

    def register(self, event: HookEvent, handler: Callable) -> None: ...
    async def fire(self, event: HookEvent, context: dict) -> None: ...
```

**WorkflowEngine 集成**: 每个节点执行前后 fire Hook，权限请求通过 Hook 拦截

**验收标准**:
- [ ] ShellExecNode 在 MANUAL 模式下弹出确认
- [ ] AUTO 模式下已批准的操作自动放行
- [ ] 权限规则可持久化到配置文件
- [ ] 自定义 Hook 可拦截节点执行 (如: 日志、通知、审批)

### 4.2 会话持久化 (W5)

**目标**: 工作流执行状态可保存/恢复/分叉

**fusion-desk 改动**:

```python
# fusion_desk/engine/session.py (新增)
@dataclass
class Session:
    session_id: str
    workflow: Workflow
    execution: WorkflowExecution
    created_at: float
    updated_at: float
    metadata: dict

class SessionStore:
    """会话存储 — SQLite 持久化。"""
    db_path: str = "~/.fusion-desk/sessions.db"

    async def save(self, session: Session) -> None: ...
    async def load(self, session_id: str) -> Session: ...
    async def list_sessions(self) -> List[Session]: ...
    async def fork(self, session_id: str) -> Session: ...
```

**CLI 集成**:
```bash
fusion-desk workflow resume <session-id>
fusion-desk workflow list-sessions
fusion-desk workflow fork <session-id>
```

**验收标准**:
- [ ] 工作流执行后自动保存会话
- [ ] `resume` 可恢复中断的工作流
- [ ] `fork` 可基于历史状态创建分支

### 4.3 流式输出 (W6)

**目标**: 工作流执行进度实时推送到 GUI

**fusion-desk 改动**:

```python
# fusion_desk/engine/events.py (新增)
class WorkflowEvent:
    event_type: str    # node_started / node_completed / node_failed / workflow_completed
    node_name: str
    data: dict
    timestamp: float

class EventEmitter:
    """事件发射器 — 支持 SSE 和 UDS 推送。"""
    async def emit(self, event: WorkflowEvent) -> None: ...
    def subscribe(self, callback) -> None: ...
```

**DeskRPCServer 集成**:
- 工作流执行时，事件通过 UDS 推送到 fusion-studio
- fusion-studio DeskView 实时更新节点状态 (颜色变化: 灰→绿/红)

**验收标准**:
- [ ] fusion-studio 中运行工作流，节点状态实时变色
- [ ] MCP HTTP 模式下，SSE 推送工具调用进度

---

## 五、M3: 生态扩展 (W7-W9)

### 5.1 插件系统 (W7)

**目标**: 第三方可开发自定义节点，动态加载

**fusion-desk 改动**:

```python
# fusion_desk/plugins/ (新增目录)
# fusion_desk/plugins/loader.py
class PluginLoader:
    """插件加载器 — 从目录/zip 动态加载。"""

    def load_from_dir(self, path: str) -> List[BaseNode]:
        """从目录加载插件节点。"""
        # 扫描 path/*.py
        # 导入模块，查找 BaseNode 子类
        # 注册到 NodeRegistry

    def load_from_zip(self, zip_path: str) -> List[BaseNode]:
        """从 zip 加载插件。"""

# 插件约定:
# my_plugin/
#   __init__.py          # 导出 MyNode
#   my_node.py           # class MyNode(BaseNode): ...
#   manifest.json        # {name, version, nodes, dependencies}
```

**CLI 集成**:
```bash
fusion-desk plugin install /path/to/plugin
fusion-desk plugin list
fusion-desk plugin uninstall <name>
```

**验收标准**:
- [ ] 自定义 Node 可通过 `plugin install` 加载
- [ ] 加载的节点出现在 `desk.list_nodes` 结果中
- [ ] 插件卸载后节点不可用

### 5.2 技能机制 (W8)

**目标**: 类似 Claude Cowork 的 /skill 斜杠命令

**fusion-desk 改动**:

```python
# fusion_desk/skills/ (新增目录)
# fusion_desk/skills/registry.py
class SkillRegistry:
    """技能注册表 — 快捷命令映射到工作流/节点。"""

    def register(self, name: str, handler: Callable, description: str) -> None: ...
    def execute(self, name: str, args: str) -> Any: ...

# 内置技能:
# /cleanup     → desktop_clean 工作流
# /classify    → ai_classify 节点
# /screenshot  → screen_capture 节点
# /search      → web_search 节点
# /organize    → download_organizer 工作流
```

**融合 NL Parser**:
- 用户输入自然语言 → nl_parser 生成工作流
- 技能名作为快捷方式，跳过 NLP 解析

**验收标准**:
- [ ] `/cleanup` 一键执行桌面清理
- [ ] 自定义技能可通过配置文件注册

### 5.3 Chrome CDP 集成 (W9)

**目标**: 通过 Chrome DevTools Protocol 实现浏览器自动化 (对标 --chrome)

**对标全量**: Claude Cowork 有 25+ CDP 子工具，Fusion-Desk 需覆盖核心子集

**方案**: 不自建浏览器，连接用户已安装的 Chrome

**fusion-desk 改动**:

```python
# fusion_desk/nodes/browser/cdp_client.py (新增)
class CDPClient:
    """Chrome DevTools Protocol 客户端。"""

    async def connect(self, port: int = 9222) -> None:
        """连接 Chrome (需 --remote-debugging-port=9222)。"""

    async def get_a11y_tree(self) -> dict:
        """获取页面无障碍树 (对标 take_snapshot)。"""

    async def click(self, selector: str) -> bool: ...
    async def fill(self, selector: str, value: str) -> bool: ...
    async def navigate(self, url: str) -> None: ...
    async def screenshot(self) -> bytes: ...
    async def execute_js(self, script: str) -> Any: ...
```

**新增 CDP 节点** (对标 Cowork 25+ 子工具的核心子集):

```python
@register_node
class CDPNavigateNode(BaseNode): ...      # 导航
@register_node
class CDPSnapshotNode(BaseNode): ...      # 获取 a11y tree (对标 take_snapshot)
@register_node
class CDPClickNode(BaseNode): ...         # 点击元素
@register_node
class CDPFillNode(BaseNode): ...          # 填写表单
@register_node
class CDPFillFormNode(BaseNode): ...      # 批量填写 (对标 fill_form)
@register_node
class CDPScreenshotNode(BaseNode): ...    # 截图
@register_node
class CDPEvaluateNode(BaseNode): ...      # 执行 JS (对标 evaluate_script)
@register_node
class CDPEmulateNode(BaseNode): ...       # 设备模拟 (viewport/geolocation/dark mode)
@register_node
class CDPNetworkNode(BaseNode): ...       # 网络请求监控 (对标 list_network_requests)
@register_node
class CDPConsoleNode(BaseNode): ...       # Console 日志 (对标 list_console_messages)
```

**W9 范围裁剪**: 10 个核心节点，覆盖 Cowork 80% 使用场景。Lighthouse/HeapSnapshot/Performance Trace 列为 P3。

**向 fusion-browser 提 issue**: 建议 FusionBrowser.app 也支持 CDP 协议端口暴露

**验收标准**:
- [ ] Chrome 以 `--remote-debugging-port=9222` 启动后，CDP 节点可控制
- [ ] CDPSnapshotNode 返回 a11y tree (可被 AI 理解)
- [ ] CDPClickNode/CDPFillNode 可操作网页元素

---

## 六、M4: 闭环增强 (W10-W12)

### 6.1 Computer Use — 鼠标键盘控制 (W10)

**目标**: 屏幕截图→AI 分析→鼠标/键盘操作的闭环

**fusion-desk 改动**:

```python
# fusion_desk/nodes/macos/input_nodes.py (新增)
@register_node
class MouseMoveNode(BaseNode):
    """移动鼠标到指定坐标。"""
    # 使用 CGEvent (pyobjc) 或 AppleScript

@register_node
class MouseClickNode(BaseNode):
    """鼠标点击 (左/右/双击)。"""

@register_node
class KeyboardTypeNode(BaseNode):
    """键盘输入文本。"""

@register_node
class KeyboardShortcutNode(BaseNode):
    """键盘快捷键 (Cmd+C, Cmd+V 等)。"""

@register_node
class ComputerUseLoopNode(BaseNode):
    """Computer Use 循环节点 — 截图→分析→操作→再截图。"""
    # 1. ScreenCaptureNode 截图
    # 2. fusion-mlx vision 分析截图
    # 3. 根据 AI 输出执行 MouseMove/Click/Type
    # 4. 循环直到目标达成或超过最大步数
```

**依赖**: pyobjc (macOS 原生事件 API)

**验收标准**:
- [ ] ComputerUseLoopNode 可完成 "打开 Safari 并搜索天气" 等任务
- [ ] 鼠标/键盘操作可精确到像素和按键

### 6.2 远程控制 (W11)

**目标**: 外部可连接运行中的 fusion-desk 会话

**fusion-desk 改动**:

```python
# fusion_desk/server/remote.py (新增)
class RemoteControlServer:
    """远程控制服务 — WebSocket 接入。"""

    # 1. 客户端连接 ws://localhost:9762/control
    # 2. 认证后可: 查看状态、提交工作流、取消任务
    # 3. 事件通过 WS 实时推送

class RemoteControlClient:
    """远程控制客户端。"""
    async def connect(self, url: str) -> None: ...
    async def submit_workflow(self, workflow: dict) -> str: ...
    async def get_status(self) -> dict: ...
```

**CLI 集成**:
```bash
fusion-desk remote serve --port 9762
fusion-desk remote connect ws://host:9762/control
```

**验收标准**:
- [ ] 远程客户端可连接并查看工作流状态
- [ ] 远程提交工作流可执行

### 6.3 结构化输出 (W12)

**目标**: 工作流/节点输出可按 JSON Schema 校验

**fusion-desk 改动**:

```python
# fusion_desk/engine/schema.py (新增)
class OutputSchema:
    """输出 Schema 校验 — 对标 --json-schema。"""

    @staticmethod
    def validate(data: dict, schema: dict) -> bool:
        """校验输出是否符合 schema。"""

# NodeResult 集成:
@dataclass
class NodeResult:
    data: dict
    schema: Optional[dict] = None  # 输出 schema

    def validate(self) -> bool:
        if self.schema:
            return OutputSchema.validate(self.data, self.schema)
        return True
```

**验收标准**:
- [ ] 节点可声明输出 schema
- [ ] 执行结果自动校验，不符合时标记为 FAILED

---

## 七、fusion-studio 改动汇总

| 阶段 | 改动 | 文件 |
|------|------|------|
| M1 | DeskView 接入 fusion-desk IPC | DeskView.swift 重构 |
| M1 | 新增 desk.* RPC 方法 | IPCClient.swift 扩展 |
| M2 | 工作流节点状态实时渲染 | DeskView + DAGCanvasView |
| M2 | 权限确认对话框 | 新增 PermissionDialog.swift |
| M2 | Hook 配置界面 | 新增 HookSettingsView.swift |
| M3 | 插件管理界面 | 新增 PluginView.swift |
| M3 | 技能命令栏 | 新增 SkillBar.swift |
| M4 | Computer Use 可视化 | 新增 ComputerUseView.swift |

---

## 八、技术风险与缓解

| 风险 | 影响 | 缓解策略 |
|------|------|----------|
| pyobjc 依赖 (Computer Use) | 增加安装复杂度 | 可选依赖，fallback 到 AppleScript |
| Chrome CDP 需要用户手动启动调试端口 | 用户体验差 | 提供一键启动脚本，向 fusion-browser 提 issue |
| UDS 权限问题 | IPC 连接失败 | socket 文件权限 666，启动时检查 |
| SQLite 并发 | 多进程写冲突 | WAL 模式，写锁队列 |
| MCP stdio 阻塞 | 主进程无法交互 | 独立子进程运行 MCP server |

---

## 九、向其他项目提 Issue 清单

| 目标项目 | Issue 标题 | 内容摘要 |
|----------|-----------|----------|
| fusion-mlx | 请求 vision/multimodal API 端点 | fusion-desk 需要图像理解能力，目前 chat 接口不原生支持图片 |
| fusion-browser | 请求 CDP 协议端口暴露 | FusionBrowser.app 应支持 `--remote-debugging-port` |
| fusion-browser | 请求 HTTP API 稳定性保证 | browser_nodes.py 依赖的 /api/browser/* 接口需版本化 |

---

## 十、里程碑验收总表

| M | 验收项 | 验收方式 |
|---|--------|----------|
| M1 | MCP stdio 注册到 Claude Code | `claude mcp add fusion-desk -- fusion-desk mcp serve --transport stdio` 成功 |
| M1 | Studio DeskView 调用后端 | 点击"运行"→ 工作流执行 → 结果回显 |
| M1 | Agent 真实执行 | `run_standard_pipeline()` 返回真实节点结果，非 "simulated" |
| M2 | 权限拦截 | ShellExecNode 在 MANUAL 模式需确认 |
| M2 | Hook 拦截 | 自定义 Hook 可拦截节点执行并修改行为 |
| M2 | 会话恢复 | 中断后 `resume` 可继续执行 |
| M2 | 流式推送 | Studio 中节点状态实时变色 |
| M3 | 插件加载 | 自定义 Node 通过 `plugin install` 可用 |
| M3 | 技能快捷 | `/cleanup` 一键执行桌面清理 |
| M3 | Chrome CDP | CDP 节点控制 Chrome 页面 (10 个核心节点) |
| M4 | Computer Use | AI 循环控制鼠标完成简单任务 |
| M4 | 远程控制 | 远程连接可提交/监控工作流 |
| M4 | 结构化输出 | 输出 schema 校验生效 |

---

## 十一、开发节奏

```
W1  ████████ MCP stdio 传输
W2  ████████ Desk IPC 连通
W3  ████████ Agent 真实执行
W4  ████████ 权限模型
W5  ████████ 会话持久化
W6  ████████ 流式输出
W7  ████████ 插件系统
W8  ████████ 技能机制
W9  ████████ Chrome CDP
W10 ████████ Computer Use
W11 ████████ 远程控制
W12 ████████ 结构化输出 + 全面测试 + 文档
```

每个 W 结束时:
1. 合并到 main 分支
2. 更新 README.md 特性列表
3. 运行 `pytest tests/` 确保通过
4. 手动验收对应里程碑项
