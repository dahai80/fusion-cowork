import pytest

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeConfig, NodeResult, NodeStatus
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus, _extract_repair_json

# CLAIM: verified by tests/test_repair_step.py


class MockRepairNode(BaseNode):
    name = "mock_repair"
    display_name = "Mock repair node"
    category = NodeCategory.IO
    description = "test: fails until params.fixed is true"
    icon = "T"
    default_label = "Mock repair"

    def __init__(self, node_id: str = "", config: NodeConfig = None, always_fail: bool = False):
        super().__init__(node_id=node_id, config=config or NodeConfig())
        self.always_fail = always_fail
        self.exec_count = 0

    async def execute(self, inputs: dict) -> NodeResult:
        self.exec_count += 1
        if not self.always_fail and bool((inputs or {}).get("fixed")):
            return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True})
        return NodeResult(status=NodeStatus.FAILED, error="mock repair failure")


class StubRepairClient:
    def __init__(self, decisions: list):
        self.decisions = list(decisions)
        self.calls = 0

    async def chat(self, model: str, messages: list, temperature: float = 0.0, max_tokens: int = 300, **kw):
        self.calls += 1
        decision = self.decisions.pop(0) if self.decisions else {"action": "abort", "params": {}}
        return type("R", (), {"content": __import__("json").dumps(decision)})()


def _wf(node: BaseNode) -> Workflow:
    wf = Workflow(name="repair")
    wf.add_node(node)
    return wf


class TestExtractRepairJson:
    def test_valid_retry(self):
        assert _extract_repair_json('{"action": "retry", "params": {"a": 1}}') == {
            "action": "retry",
            "params": {"a": 1},
        }

    def test_wrapped_in_prose(self):
        assert _extract_repair_json('sure {"action": "skip"} done')["action"] == "skip"

    def test_no_json_is_abort(self):
        assert _extract_repair_json("no json here")["action"] == "abort"

    def test_unknown_action_is_abort(self):
        assert _extract_repair_json('{"action": "hack"}')["action"] == "abort"

    def test_non_dict_params_is_empty(self):
        assert _extract_repair_json('{"action": "retry", "params": "x"}')["params"] == {}


class TestRepairStep:
    @pytest.mark.asyncio
    async def test_default_off_no_llm_call(self):
        node = MockRepairNode(node_id="n1", config=NodeConfig(max_retries=1, retry_on_business_failure=True))
        client = StubRepairClient([])
        engine = WorkflowEngine(repair_client=client)
        execution = await engine.execute(_wf(node))
        assert execution.status == WorkflowStatus.FAILED
        assert client.calls == 0

    @pytest.mark.asyncio
    async def test_repair_retry_succeeds_with_params(self):
        node = MockRepairNode(node_id="n1", config=NodeConfig(on_error_repair=True))
        client = StubRepairClient([{"action": "retry", "params": {"fixed": True}}])
        engine = WorkflowEngine(repair_client=client)
        execution = await engine.execute(_wf(node))
        assert execution.status == WorkflowStatus.SUCCESS
        assert client.calls == 1
        assert node.exec_count == 2
        assert execution.steps[0].output_data.get("ok") is True
        assert execution.steps[0].output_data.get("_repair", {}).get("action") == "retry"

    @pytest.mark.asyncio
    async def test_repair_skip_continues_run(self):
        node = MockRepairNode(node_id="n1", config=NodeConfig(on_error_repair=True))
        client = StubRepairClient([{"action": "skip", "params": {}}])
        engine = WorkflowEngine(repair_client=client)
        execution = await engine.execute(_wf(node))
        assert execution.status == WorkflowStatus.SUCCESS
        assert execution.steps[0].status == NodeStatus.SKIPPED
        assert execution.steps[0].summary == "llm_repair: skip"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_abort(self):
        node = MockRepairNode(node_id="n1", config=NodeConfig(on_error_repair=True))
        client = StubRepairClient([])

        async def broken_chat(*a, **kw):
            raise RuntimeError("llm down")

        client.chat = broken_chat
        engine = WorkflowEngine(repair_client=client)
        execution = await engine.execute(_wf(node))
        assert execution.status == WorkflowStatus.FAILED
        assert client.calls == 0

    @pytest.mark.asyncio
    async def test_repair_is_one_shot(self):
        node = MockRepairNode(node_id="n1", config=NodeConfig(on_error_repair=True), always_fail=True)
        client = StubRepairClient([{"action": "retry", "params": {"fixed": True}}])
        engine = WorkflowEngine(repair_client=client)
        execution = await engine.execute(_wf(node))
        assert execution.status == WorkflowStatus.FAILED
        assert client.calls == 1
        assert node.exec_count == 2

    @pytest.mark.asyncio
    async def test_repair_abort_preserves_failure(self):
        node = MockRepairNode(node_id="n1", config=NodeConfig(on_error_repair=True), always_fail=True)
        client = StubRepairClient([{"action": "abort", "params": {}}])
        engine = WorkflowEngine(repair_client=client)
        execution = await engine.execute(_wf(node))
        assert execution.status == WorkflowStatus.FAILED
        assert node.exec_count == 1

    def test_config_roundtrip(self):
        node = MockRepairNode(node_id="n1", config=NodeConfig(on_error_repair=True))
        data = node.to_dict()
        assert data["config"]["on_error_repair"] is True
        restored = MockRepairNode.from_dict(data)
        assert restored.config.on_error_repair is True
        default = MockRepairNode(node_id="n2").to_dict()
        assert default["config"]["on_error_repair"] is False
