# Fusion-Cowork API Reference

## Engine

### `Workflow`
```python
from fusion_cowork import Workflow

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
from fusion_cowork import EnhancedScheduler
scheduler = EnhancedScheduler(task_scheduler)
stats = scheduler.get_stats(days=7)
calendar = scheduler.get_calendar_data(2026, 7)
```

### `WorkflowOptimizer` (V0.2)
```python
from fusion_cowork import WorkflowOptimizer
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
from fusion_cowork import AgentOrchestrator, Agent, AgentRole
orc = AgentOrchestrator()
orc.register_agent(Agent("agent1", "Planner", AgentRole.PLANNER))
plan = await orc.create_plan("analysis", "Analyze files")
orc.add_task(plan.plan_id, "agent1", "Plan execution", {"input": "data"})
result = await orc.execute_plan(plan.plan_id)
```

## Server

### MCP Server (V0.2)
```python
from fusion_cowork import MCPServer
server = MCPServer(port=9761)
await server.start()  # 15 tools exposed
tools = server.get_tools_list()
```

### CrossDeviceSync (V0.3)
```python
from fusion_cowork import CrossDeviceSync
sync = CrossDeviceSync(port=9760)
await sync.start()
await sync.sync_workflow(workflow_data)
```

## Report Generator (V0.2)
```python
from fusion_cowork import ReportGenerator
gen = ReportGenerator()
report = gen.generate_workflow_report(execution, format="html")
gen.save_report(report, "~/Desktop/reports")
```

## CLI Reference
```bash
fusion-cowork template list/run/show
fusion-cowork ai generate/status
fusion-cowork workflow run/list
fusion-cowork schedule list/add/remove
fusion-cowork system info/clean
fusion-cowork browser start/open/extract
fusion-cowork mcp start               # V0.2
fusion-cowork report generate          # V0.2
fusion-cowork orchestrator run         # V0.3
```