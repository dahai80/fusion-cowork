"""issue #182: 节点重试尝试在 run steps API 可观测 — step.attempt + attempt_errors + retry_on_business_failure opt-in。"""

from __future__ import annotations

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine


@register_node
class _FlakyRaiseNode(BaseNode):
    name = "flaky_raise_182"
    display_name = "Flaky Raise 182"
    category = NodeCategory.TOOL
    description = "测试用: 前两次抛异常, 第三次成功"
    icon = "🎲"
    default_label = "Flaky Raise"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        if type(self).calls < 3:
            raise RuntimeError(f"transient-{type(self).calls}")
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@register_node
class _BusinessFailNode(BaseNode):
    name = "biz_fail_182"
    display_name = "Biz Fail 182"
    category = NodeCategory.TOOL
    description = "测试用: 业务 FAILED 两次后成功"
    icon = "📉"
    default_label = "Biz Fail"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        if type(self).calls < 3:
            return NodeResult(status=NodeStatus.FAILED, error=f"业务失败-{type(self).calls}")
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


def _wf(node_name: str, config: NodeConfig):
    wf = Workflow(name="retry-wf-182")
    node = NodeRegistry.create(node_name)
    node.config = config
    wf.add_node(node)
    return wf


class TestAttemptObservability:
    async def test_exception_retry_records_attempts(self):
        _FlakyRaiseNode.calls = 0
        eng = WorkflowEngine()
        config = NodeConfig(max_retries=3, retry_delay=0.0)
        execution = await eng.execute(_wf("flaky_raise_182", config), execution_id="run-182-raise")
        assert execution.status.value == "success"
        step = execution.steps[0]
        assert step.attempt == 3
        assert len(step.attempt_errors) == 2
        assert "transient-1" in step.attempt_errors[0]
        assert "transient-2" in step.attempt_errors[1]

    async def test_success_first_try_clean(self):
        _FlakyRaiseNode.calls = 2  # 下次调用即成功
        eng = WorkflowEngine()
        config = NodeConfig(max_retries=3, retry_delay=0.0)
        execution = await eng.execute(_wf("flaky_raise_182", config), execution_id="run-182-clean")
        step = execution.steps[0]
        assert step.attempt == 1
        assert step.attempt_errors == []

    async def test_business_failure_no_retry_by_default(self):
        _BusinessFailNode.calls = 0
        eng = WorkflowEngine()
        config = NodeConfig(max_retries=3, retry_delay=0.0)
        execution = await eng.execute(_wf("biz_fail_182", config), execution_id="run-182-biz-default")
        assert execution.status.value == "failed"
        step = execution.steps[0]
        assert step.attempt == 1
        assert _BusinessFailNode.calls == 1

    async def test_business_failure_retries_when_opted_in(self):
        _BusinessFailNode.calls = 0
        eng = WorkflowEngine()
        config = NodeConfig(max_retries=3, retry_delay=0.0, retry_on_business_failure=True)
        execution = await eng.execute(_wf("biz_fail_182", config), execution_id="run-182-biz-optin")
        assert execution.status.value == "success"
        step = execution.steps[0]
        assert step.attempt == 3
        assert _BusinessFailNode.calls == 3
        assert len(step.attempt_errors) == 2
        assert "业务失败-1" in step.attempt_errors[0]

    async def test_exhausted_retries_surface_all_errors(self):
        _BusinessFailNode.calls = 0

        @register_node
        class _AlwaysBizFailNode(BaseNode):
            name = "always_biz_fail_182"
            display_name = "Always Biz Fail 182"
            category = NodeCategory.TOOL
            description = "测试用: 永远业务失败"
            icon = "❌"
            default_label = "Always Fail"

            async def execute(self, inputs: dict) -> NodeResult:
                return NodeResult(status=NodeStatus.FAILED, error="永远失败")

        eng = WorkflowEngine()
        config = NodeConfig(max_retries=2, retry_delay=0.0, retry_on_business_failure=True)
        execution = await eng.execute(_wf("always_biz_fail_182", config), execution_id="run-182-biz-exhaust")
        assert execution.status.value == "failed"
        step = execution.steps[0]
        assert step.attempt == 3
        assert step.attempt_errors == ["永远失败", "永远失败", "永远失败"]

    async def test_step_dict_exposes_attempt_fields(self):
        from fusion_cowork.server.web_api import RunRegistry

        _FlakyRaiseNode.calls = 0
        eng = WorkflowEngine()
        config = NodeConfig(max_retries=3, retry_delay=0.0)
        execution = await eng.execute(_wf("flaky_raise_182", config), execution_id="run-182-dict")
        d = RunRegistry._step_dict(execution.steps[0])
        assert d["attempt"] == 3
        assert len(d["attempt_errors"]) == 2

    def test_node_config_roundtrip(self):
        from fusion_cowork.nodes.tools.tool_nodes import ShellExecNode

        base = ShellExecNode(
            node_id="n1",
            config=NodeConfig(max_retries=2, retry_on_business_failure=True, params={"command": "true"}),
        )
        dumped = base.to_dict()
        assert dumped["config"]["retry_on_business_failure"] is True
        restored = ShellExecNode.from_dict(dumped)
        assert restored.config.retry_on_business_failure is True
        assert restored.config.max_retries == 2
