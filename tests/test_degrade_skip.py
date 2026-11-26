import pytest

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeConfig, NodeResult, NodeStatus
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus

# CLAIM: verified by tests/test_degrade_skip.py


class MockEchoNode(BaseNode):
    name = "mock_echo"
    display_name = "Mock echo node"
    category = NodeCategory.IO
    description = "test: echoes inputs"
    icon = "E"
    default_label = "Mock echo"

    def __init__(self, node_id: str = "", config: NodeConfig = None):
        super().__init__(node_id=node_id, config=config or NodeConfig())
        self.last_inputs = None
        self.exec_count = 0

    async def execute(self, inputs: dict) -> NodeResult:
        self.exec_count += 1
        self.last_inputs = dict(inputs or {})
        return NodeResult(status=NodeStatus.SUCCESS, data={"echo": self.last_inputs})


class MockFlagNode(BaseNode):
    name = "mock_flag"
    display_name = "Mock flag node"
    category = NodeCategory.IO
    description = "test: outputs a flag"
    icon = "F"
    default_label = "Mock flag"

    def __init__(self, node_id: str = "", config: NodeConfig = None, flag: bool = False):
        super().__init__(node_id=node_id, config=config or NodeConfig())
        self.flag = flag

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": self.flag})


def _wf(*nodes) -> Workflow:
    wf = Workflow(name="degrade")
    for n in nodes:
        wf.add_node(n)
    if len(nodes) == 2:
        wf.connect(nodes[0].id, nodes[1].id)
    return wf


# skip_if that always hits: missing path (None) != "never"
HIT = [{"path": "nothing_here", "not_equals": "never"}]


class TestDegradeOnSkip:
    @pytest.mark.asyncio
    async def test_default_off_skips_unchanged(self):
        node = MockEchoNode(node_id="n1", config=NodeConfig(skip_if=HIT))
        engine = WorkflowEngine()
        execution = await engine.execute(_wf(node))
        assert execution.status == WorkflowStatus.SUCCESS
        assert execution.steps[0].status == NodeStatus.SKIPPED
        assert execution.steps[0].output_data.get("skip_if")
        assert "_degraded" not in execution.steps[0].output_data
        assert node.exec_count == 0

    @pytest.mark.asyncio
    async def test_degrade_runs_on_partial_inputs(self):
        node = MockEchoNode(node_id="n1", config=NodeConfig(skip_if=HIT, degrade_on_skip=True))
        engine = WorkflowEngine()
        execution = await engine.execute(_wf(node))
        assert execution.status == WorkflowStatus.SUCCESS
        assert execution.steps[0].status == NodeStatus.SUCCESS
        assert node.exec_count == 1
        assert execution.steps[0].output_data.get("_degraded")
        assert "degraded" in (execution.steps[0].summary or "")

    @pytest.mark.asyncio
    async def test_degrade_not_tagged_when_condition_misses(self):
        node = MockEchoNode(
            node_id="n1", config=NodeConfig(skip_if=[{"path": "nothing_here", "equals": "never"}], degrade_on_skip=True)
        )
        engine = WorkflowEngine()
        execution = await engine.execute(_wf(node))
        assert execution.steps[0].status == NodeStatus.SUCCESS
        assert "_degraded" not in execution.steps[0].output_data

    @pytest.mark.asyncio
    async def test_degrade_after_upstream_skipped(self):
        # producer skips (its own skip_if hits) -> sink skip_if on producer
        # output hits (missing output); degrade_on_skip runs the sink anyway
        prod = MockFlagNode(node_id="prod", config=NodeConfig(skip_if=HIT), flag=False)
        sink = MockEchoNode(
            node_id="sink",
            config=NodeConfig(skip_if=[{"source": "prod", "path": "ok", "not_equals": "never"}], degrade_on_skip=True),
        )
        engine = WorkflowEngine()
        execution = await engine.execute(_wf(prod, sink))
        assert execution.status == WorkflowStatus.SUCCESS
        assert execution.steps[0].status == NodeStatus.SKIPPED
        assert sink.exec_count == 1
        sink_step = execution.steps[1]
        assert sink_step.status == NodeStatus.SUCCESS
        assert sink_step.output_data.get("_degraded")
        # partial inputs: producer output absent
        assert "ok" not in (sink.last_inputs or {}).get("echo", sink.last_inputs or {})

    def test_config_roundtrip(self):
        node = MockEchoNode(node_id="n1", config=NodeConfig(skip_if=HIT, degrade_on_skip=True))
        data = node.to_dict()
        clone = MockEchoNode.from_dict(data)
        assert clone.config.degrade_on_skip is True
        node2 = MockEchoNode(node_id="n2", config=NodeConfig())
        assert MockEchoNode.from_dict(node2.to_dict()).config.degrade_on_skip is False
