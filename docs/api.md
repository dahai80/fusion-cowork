# Fusion-Desk API Reference

## Engine

### `Workflow`
```python
from fusion_desk import Workflow

wf = Workflow(name="my_workflow", description="Automate desktop")
wf.add_node(my_node)
wf.connect("node1", "node2")
wf.to_json()
```

### `WorkflowEngine`
```python
engine = WorkflowEngine()
execution = await engine.execute(wf)
print(execution.status, execution.total_time)
```

### `EnhancedScheduler` (V0.2)
```python
from fusion_desk import EnhancedScheduler
scheduler = EnhancedScheduler(task_scheduler)
stats = scheduler.get_stats(days=7)
calendar = scheduler.get_calendar_data(2026, 7)
```

### `WorkflowOptimizer` (V0.2)
```python
from fusion_desk import WorkflowOptimizer
optimizer = WorkflowOptimizer()
analysis = optimizer.analyze_workflow(history)
print(analysis.score, analysis.bottleneck_nodes)
```

## Nodes

### macOS System
- `DesktopCleanNode` — organize desktop
- `DownloadOrganizerNode` — archive downloads
- `DiskCleanerNode` — clean caches
- `ScreenCaptureNode` — screenshot (V0.2)
- `ClipboardNode` — clipboard read/write (V0.2)
- `NotificationNode` — macOS notifications (V0.2)
- `AppLifecycleNode` — app launch/quit (V0.2)
- `OCRNode` — text recognition (V0.2)

### AI Processing
- `AIClassifyNode` — semantic file classification
- `AISummarizeNode` — document summarization
- `AIGenerateNameNode` — intelligent file naming

### Tools
- `ShellExecNode` — shell commands
- `PythonREPLNode` — Python execution
- `WebSearchNode` — DuckDuckGo search
- `FetchURLNode` — fetch web pages
- `ApplyEditNode` — find/replace edits

### Browser (V0.1)
- `BrowserOpenNode` — open URL in browser
- `BrowserExtractNode` — extract page content
- `BrowserAutomateNode` — click/fill/screenshot

## Orchestrator (V0.3)
```python
from fusion_desk import AgentOrchestrator, Agent, AgentRole
orc = AgentOrchestrator()
orc.register_agent(Agent("agent1", "Planner", AgentRole.PLANNER))
plan = await orc.create_plan("analysis", "Analyze files")
orc.add_task(plan.plan_id, "agent1", "Plan execution", {"input": "data"})
result = await orc.execute_plan(plan.plan_id)
```

## Server

### MCP Server (V0.2)
```python
from fusion_desk import MCPServer
server = MCPServer(port=9761)
await server.start()  # 15 tools exposed
tools = server.get_tools_list()
```

### CrossDeviceSync (V0.3)
```python
from fusion_desk import CrossDeviceSync
sync = CrossDeviceSync(port=9760)
await sync.start()
await sync.sync_workflow(workflow_data)
```

## Report Generator (V0.2)
```python
from fusion_desk import ReportGenerator
gen = ReportGenerator()
report = gen.generate_workflow_report(execution, format="html")
gen.save_report(report, "~/Desktop/reports")
```

## CLI Reference
```bash
fusion-desk template list/run/show
fusion-desk ai generate/status
fusion-desk workflow run/list
fusion-desk schedule list/add/remove
fusion-desk system info/clean
fusion-desk browser start/open/extract
fusion-desk mcp start               # V0.2
fusion-desk report generate          # V0.2
fusion-desk orchestrator run         # V0.3
```