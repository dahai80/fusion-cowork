from types import SimpleNamespace

import pytest

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeConfig, NodeResult, NodeStatus
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus, _skip_if_hit
from fusion_cowork.trajectory.cond_distill import build_evidence, extract_plan, plan_to_workflow

# CLAIM: verified by tests/test_cond_distill.py


class _Producer(BaseNode):
    name = "risk_probe"
    display_name = "risk probe"
    category = NodeCategory.IO
    description = "test: emits a risk value"
    icon = "T"
    default_label = "risk probe"

    def __init__(self, node_id: str = "", config: NodeConfig = None, risk: int = 8):
        super().__init__(node_id=node_id, config=config or NodeConfig())
        self.risk = risk

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"risk": self.risk})


class _Sink(BaseNode):
    name = "sink"
    display_name = "sink"
    category = NodeCategory.IO
    description = "test: records it ran"
    icon = "T"
    default_label = "sink"

    def __init__(self, node_id: str = "", config: NodeConfig = None):
        super().__init__(node_id=node_id, config=config or NodeConfig())
        self.ran = False

    async def execute(self, inputs: dict) -> NodeResult:
        self.ran = True
        return NodeResult(status=NodeStatus.SUCCESS, data={"done": True})


class TestBuildEvidence:
    def test_from_dicts(self):
        steps = [{"node_name": "a", "status": "success", "output_data": {"x": 1}, "error": ""}]
        ev = build_evidence(steps, run_success=True)
        assert ev == [
            {
                "node": "a",
                "id": "a",
                "upstream": [],
                "input_bindings": {},
                "status": "success",
                "output": {"x": 1},
                "error": "",
            }
        ]

    def test_from_step_objects(self):
        s = SimpleNamespace(node_name="b", status="failed", output_data=None, error="boom")
        ev = build_evidence([s], run_success=False)
        assert ev[0]["node"] == "b" and ev[0]["error"] == "boom"

    def test_output_truncation(self):
        s = SimpleNamespace(node_name="c", status="success", output_data={"k": "v" * 500}, error="")
        ev = build_evidence([s], run_success=True)
        assert len(ev[0]["output"]["k"]) <= 200


class TestExtractPlan:
    @pytest.mark.asyncio
    async def test_parses_llm_reply(self):
        class Stub:
            async def chat(self, model, messages, temperature=0.0, max_tokens=0, **kw):
                return SimpleNamespace(
                    content='{"steps": [{"node": "risk_probe"}, {"node": "sink", "when": {"var": "risk_probe.risk", "gte": 6}}]}'
                )

        plan = await extract_plan(Stub(), [{"node": "risk_probe"}])
        assert len(plan["steps"]) == 2
        assert plan["steps"][1]["when"]["gte"] == 6

    async def test_input_bindings_annotated_deterministically(self):
        # issue #638 follow-up: routed ports come from recorded bindings, not
        # LLM inference — extract_plan copies them onto the plan steps
        class Stub:
            async def chat(self, model, messages, temperature=0.0, max_tokens=0, **kw):
                return SimpleNamespace(content='{"steps": [{"node": "sink", "id": "s1"}]}')

        evidence = [
            {
                "node": "sink",
                "id": "s1",
                "input_bindings": {"data": ["f1", "passed"]},
            }
        ]
        plan = await extract_plan(Stub(), evidence)
        assert plan["steps"][0]["inputs"] == {"data": "f1.passed"}

    @pytest.mark.asyncio
    async def test_junk_reply_is_empty_plan(self):
        class Stub:
            async def chat(self, model, messages, **kw):
                return SimpleNamespace(content="no json at all")

        plan = await extract_plan(Stub(), [])
        assert plan == {"steps": []}


class TestPlanToWorkflow:
    def test_linear_chain_and_skip_if_inversion(self):
        wf = Workflow(name="compiled")
        created = []

        def create_node(name):
            n = _Sink(node_id=f"{name}_1")
            created.append(name)
            return n

        plan = {
            "steps": [
                {"node": "risk_probe"},
                {"node": "sink", "when": {"var": "risk_probe.risk", "gte": 6}},
            ]
        }
        ids = plan_to_workflow(plan, create_node, wf)
        assert created == ["risk_probe", "sink"]
        assert len(ids) == 2
        assert wf.nodes[ids[1]].config.skip_if == [{"path": "risk", "negate": True, "source": ids[0], "gte": 6}]

    def test_fan_out_fan_in_preserved(self):
        # issue #638: recorded upstream lists compile back as the DAG shape
        wf = Workflow(name="compiled")
        created = []

        def create_node(name):
            n = _Sink(node_id=name)
            created.append(name)
            return n

        plan = {
            "steps": [
                {"node": "src", "id": "src", "upstream": []},
                {"node": "br_a", "id": "br_a", "upstream": ["src"]},
                {"node": "br_b", "id": "br_b", "upstream": ["src"]},
                {"node": "join", "id": "join", "upstream": ["br_a", "br_b"]},
            ]
        }
        ids = plan_to_workflow(plan, create_node, wf)
        edge_pairs = {(e.source_id, e.target_id) for e in wf.edges}
        assert (ids[0], ids[1]) in edge_pairs
        assert (ids[0], ids[2]) in edge_pairs
        assert (ids[1], ids[3]) in edge_pairs
        assert (ids[2], ids[3]) in edge_pairs
        # no spurious linear chain edge src -> br_b or src -> join
        assert (ids[0], ids[3]) not in edge_pairs

    def test_dag_step_without_upstream_chains_to_prev(self):
        # upstream outside the plan falls back to prev-chaining so the step stays reachable
        wf = Workflow(name="compiled")

        def create_node(name):
            return _Sink(node_id=name)

        plan = {
            "steps": [
                {"node": "a", "id": "a", "upstream": []},
                {"node": "b", "id": "b", "upstream": ["ghost"]},
            ]
        }
        ids = plan_to_workflow(plan, create_node, wf)
        assert [(e.source_id, e.target_id) for e in wf.edges] == [(ids[0], ids[1])]

    def test_duplicate_instance_id_skipped(self):
        wf = Workflow(name="compiled")
        calls = []

        def create_node(name):
            calls.append(name)
            return _Sink(node_id=name)

        plan = {"steps": [{"node": "a", "id": "x", "upstream": []}, {"node": "a", "id": "x", "upstream": []}]}
        ids = plan_to_workflow(plan, create_node, wf)
        assert calls == ["a"]
        assert len(ids) == 1


class _PortSink(BaseNode):
    name = "port_sink"
    display_name = "port sink"
    category = NodeCategory.IO
    description = "test: single declared input port"
    icon = "T"
    default_label = "port sink"
    inputs = [{"key": "data", "label": "data", "type": "any"}]

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"done": True})


class TestPlanToWorkflowPortRouting:
    def test_input_field_routes_edge_port(self):
        # issue #638 follow-up: "input": "<upstream>.<output_key>" compiles to
        # a routed edge; target port resolved from declared node inputs
        wf = Workflow(name="routed")

        def create_node(name):
            return _Producer(node_id=name) if name == "a" else _PortSink(node_id=name)

        plan = {
            "steps": [
                {"node": "a", "id": "a", "upstream": []},
                {"node": "sink", "id": "b", "upstream": ["a"], "input": "a.risk"},
            ]
        }
        ids = plan_to_workflow(plan, create_node, wf)
        edge = wf.edges[0]
        assert (edge.source_id, edge.target_id) == (ids[0], ids[1])
        assert edge.source_output == "risk"
        assert edge.target_input == "data"

    def test_inputs_map_routes_explicit_port(self):
        wf = Workflow(name="routed2")
        plan = {
            "steps": [
                {"node": "a", "id": "a", "upstream": []},
                {"node": "sink", "id": "b", "upstream": ["a"], "inputs": {"payload": "a.risk"}},
            ]
        }
        plan_to_workflow(plan, lambda n: _PortSink(node_id=n), wf)
        edge = wf.edges[0]
        assert edge.source_output == "risk"
        assert edge.target_input == "payload"

    def test_no_routing_keeps_default_edge(self):
        wf = Workflow(name="routed3")
        plan = {
            "steps": [
                {"node": "a", "id": "a", "upstream": []},
                {"node": "sink", "id": "b", "upstream": ["a"]},
            ]
        }
        plan_to_workflow(plan, lambda n: _PortSink(node_id=n), wf)
        edge = wf.edges[0]
        assert edge.source_output == "output"
        assert edge.target_input == "input"

    def test_routing_to_unknown_upstream_ignored(self):
        # "input" referencing an id outside the plan must not crash or misroute
        wf = Workflow(name="routed4")
        plan = {
            "steps": [
                {"node": "a", "id": "a", "upstream": []},
                {"node": "sink", "id": "b", "upstream": ["a"], "input": "ghost.risk"},
            ]
        }
        plan_to_workflow(plan, lambda n: _PortSink(node_id=n), wf)
        edge = wf.edges[0]
        assert edge.source_output == "output"
        assert edge.target_input == "input"


class TestSkipIfNumericOps:
    def test_gte_hit_and_miss(self):
        data = {"n1": {"risk": 8}}
        miss = {"n1": {"risk": 3}}
        assert _skip_if_hit([{"source": "n1", "path": "risk", "gte": 6}], data, {}) != ""
        assert _skip_if_hit([{"source": "n1", "path": "risk", "gte": 6}], miss, {}) == ""

    def test_lte(self):
        data = {"n1": {"risk": 3}}
        assert _skip_if_hit([{"source": "n1", "path": "risk", "lte": 5}], data, {}) != ""
        assert _skip_if_hit([{"source": "n1", "path": "risk", "lte": 5}], {}, {}) == ""

    def test_negate(self):
        data = {"n1": {"risk": 8}}
        assert _skip_if_hit([{"source": "n1", "path": "risk", "gte": 6, "negate": True}], data, {}) == ""
        assert _skip_if_hit([{"source": "n1", "path": "risk", "gte": 6, "negate": True}], {}, {}) != ""

    def test_non_numeric_value_is_not_hit(self):
        data = {"n1": {"risk": "high"}}
        assert _skip_if_hit([{"source": "n1", "path": "risk", "gte": 6}], data, {}) == ""


class TestCompiledWorkflowRuns:
    @pytest.mark.asyncio
    async def test_condition_true_runs_sink(self):
        wf = Workflow(name="flywheel")
        plan = {"steps": [{"node": "risk_probe"}, {"node": "sink", "when": {"var": "risk_probe.risk", "gte": 6}}]}
        producer = _Producer(node_id="risk_probe_1", risk=8)
        sink = _Sink(node_id="sink_1")
        nodes = {"risk_probe": producer, "sink": sink}

        def create_node(name):
            return nodes[name]

        plan_to_workflow(plan, create_node, wf)
        engine = WorkflowEngine()
        execution = await engine.execute(wf)
        assert execution.status == WorkflowStatus.SUCCESS
        assert sink.ran is True

    @pytest.mark.asyncio
    async def test_condition_false_skips_sink(self):
        wf = Workflow(name="flywheel")
        plan = {"steps": [{"node": "risk_probe"}, {"node": "sink", "when": {"var": "risk_probe.risk", "gte": 6}}]}
        producer = _Producer(node_id="risk_probe_1", risk=2)
        sink = _Sink(node_id="sink_1")
        nodes = {"risk_probe": producer, "sink": sink}

        def create_node(name):
            return nodes[name]

        plan_to_workflow(plan, create_node, wf)
        engine = WorkflowEngine()
        execution = await engine.execute(wf)
        assert execution.status == WorkflowStatus.SUCCESS
        assert sink.ran is False
        assert execution.steps[1].status == NodeStatus.SKIPPED
