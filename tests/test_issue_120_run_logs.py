"""#120: run-scoped 结构化执行日志 — grep "[<run_id>]" 重建时间线。"""

from __future__ import annotations

import logging

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import (
    Workflow,
    WorkflowEngine,
    _node_verdict,
    _sanitize_log_value,
    _sanitize_params,
)
from fusion_cowork.orchestrator.orchestrator import Agent, AgentOrchestrator, AgentRole, AgentTask


@register_node
class _EchoNode(BaseNode):
    name = "echo_120"
    display_name = "Echo 120"
    category = NodeCategory.TOOL
    description = "测试用: 回显输入"
    icon = "🔁"
    default_label = "Echo"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"out": inputs.get("in", "")}, summary="ok")


@register_node
class _FailNode(BaseNode):
    name = "fail_120"
    display_name = "Fail 120"
    category = NodeCategory.TOOL
    description = "测试用: 总是失败"
    icon = "💥"
    default_label = "Fail"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.FAILED, error="boom")


@register_node
class _BlockedNode(BaseNode):
    name = "blocked_120"
    display_name = "Blocked 120"
    category = NodeCategory.TOOL
    description = "测试用: 输出 blocked 标记"
    icon = "⛔"
    default_label = "Blocked"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"blocked": True})


class _DenyAll:
    async def check(self, node_name, action, data):
        return False


def _wf(*names):
    wf = Workflow(name="log-wf")
    prev = ""
    for n in names:
        node = NodeRegistry.create(n)
        nid = wf.add_node(node)
        if prev:
            wf.connect(prev, nid)
        prev = nid
    return wf


class TestSanitize:
    def test_scalars_kept_strings_capped(self):
        assert _sanitize_log_value(3) == 3
        assert _sanitize_log_value(True) is True
        assert len(_sanitize_log_value("x" * 500)) == 200

    def test_complex_summarized(self):
        assert _sanitize_log_value({"a": 1}) == "<dict:1>"
        assert _sanitize_log_value([1, 2]) == "<list:2>"

    def test_params_secret_redacted(self):
        import json as _json

        out = _json.loads(_sanitize_params({"api_key": "sk-live", "q": "ok", "payload": {"deep": 1}}))
        assert out["api_key"] == "***"
        assert out["q"] == "ok"
        assert out["payload"] == "<dict:1>"

    def test_params_key_cap(self):
        import json as _json

        out = _json.loads(_sanitize_params({f"k{i}": i for i in range(30)}))
        assert out["..."] == "<+10 more>"


class TestVerdict:
    def test_mapping(self):
        assert _node_verdict(NodeStatus.FAILED, {}) == "failed"
        assert _node_verdict(NodeStatus.DENIED, {}) == "gated"
        assert _node_verdict(NodeStatus.SUCCESS, {"blocked": True}) == "blocked"
        assert _node_verdict(NodeStatus.SUCCESS, {"passed": False}) == "gated"
        assert _node_verdict(NodeStatus.SUCCESS, {"gated": True}) == "gated"
        assert _node_verdict(NodeStatus.SUCCESS, {"ok": 1}) == "success"


class TestEngineLogs:
    async def test_success_timeline(self, caplog):
        caplog.set_level(logging.INFO, logger="fusion_cowork.engine.workflow")
        eng = WorkflowEngine()
        wf = _wf("echo_120")
        run_id = "run-120-ok"
        await eng.execute(wf, execution_id=run_id)
        text = [r.getMessage() for r in caplog.records]
        # issue #187: run start 行追加 hitl_mode/hitl_timeout 字段
        # issue #189: run start 行追加 concurrency/executors 字段
        assert (
            f"[{run_id}] run start: workflow=log-wf nodes=1 hitl_mode=headless hitl_timeout=1800s "
            "concurrency=1 executors={'simple': 1}"
        ) in text
        assert f"[{run_id}] step start: node=echo_120 node_id=" in "".join(text)
        assert any("verdict=success" in m and f"[{run_id}] step end" in m for m in text)
        assert f"[{run_id}] run end:    outcome=success" in "".join(text)

    async def test_failed_run_identifiable(self, caplog):
        caplog.set_level(logging.INFO, logger="fusion_cowork.engine.workflow")
        eng = WorkflowEngine()
        run_id = "run-120-fail"
        execution = await eng.execute(_wf("fail_120"), execution_id=run_id)
        assert execution.status.value == "failed"
        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("verdict=failed" in m and "error=boom" in m for m in warns)

    async def test_blocked_verdict(self, caplog):
        caplog.set_level(logging.INFO, logger="fusion_cowork.engine.workflow")
        eng = WorkflowEngine()
        await eng.execute(_wf("blocked_120"), execution_id="run-120-block")
        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("verdict=blocked" in m for m in warns)

    async def test_permission_denied_gated(self, caplog):
        caplog.set_level(logging.INFO, logger="fusion_cowork.engine.workflow")
        eng = WorkflowEngine(permission_manager=_DenyAll())
        execution = await eng.execute(_wf("echo_120"), execution_id="run-120-deny")
        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("verdict=gated" in m and "权限拒绝" in m for m in warns)
        assert execution.status.value == "failed"


class TestOrchestratorLogs:
    async def test_task_start_end(self, caplog):
        caplog.set_level(logging.INFO, logger="fusion_cowork.orchestrator.orchestrator")
        orch = AgentOrchestrator()
        orch._agents["a1"] = Agent(agent_id="a1", name="agent", role=AgentRole.EXECUTOR)
        orch._executors["a1"] = lambda data: {"status": "ok"}
        task = AgentTask(task_id="task-120", agent_id="a1", description="d")
        await orch._run_submitted_task(task)
        text = [r.getMessage() for r in caplog.records]
        assert "[task-120] task start: agent=a1 desc=d" in text
        assert "[task-120] task end: outcome=completed total=" in "".join(text)
