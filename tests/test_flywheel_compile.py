import json
from types import SimpleNamespace

import pytest

from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeConfig, NodeResult, NodeStatus
from fusion_cowork.engine.workflow import Workflow
from fusion_cowork.trajectory.compile import compile_workflow, steps_from_jsonl, steps_from_snapshot

# CLAIM: verified by tests/test_flywheel_compile.py


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


def _make_factory():
    def create_node(name: str) -> BaseNode:
        if name == "risk_probe":
            return _Producer()
        if name == "sink":
            return _Sink()
        raise ValueError(f"unknown node: {name}")

    return create_node


def _record(run_id, node_name, status, data=None, error=""):
    return {
        "run_id": run_id,
        "node_id": f"n_{node_name}",
        "ts": 1700000000000,
        "event": "tool_result",
        "payload": {
            "legacy_event": "post_node_execute",
            "execution_id": run_id,
            "node_name": node_name,
            "error": error,
            "data": {"result": {"status": status, "data": data or {}, "error": error}},
        },
        "duration_ms": 5,
        "status": status,
    }


class TestStepsFromJsonl:
    def test_recovers_output_data_and_skips_other_events(self, tmp_path):
        p = tmp_path / "traj.jsonl"
        lines = [
            json.dumps({"run_id": "r1", "event": "start", "payload": {"legacy_event": "workflow_start"}, "status": ""}),
            json.dumps(_record("r1", "risk_probe", "success", {"risk": 8})),
            json.dumps(
                {"run_id": "r1", "event": "tool_result", "payload": {"legacy_event": "notification"}, "status": ""}
            ),
            json.dumps(_record("r1", "sink", "success", {"done": True})),
        ]
        p.write_text("\n".join(lines) + "\n")
        steps = steps_from_jsonl(p)
        assert [s["node_name"] for s in steps] == ["risk_probe", "sink"]
        assert steps[0]["output_data"] == {"risk": 8}
        assert steps[0]["status"] == "success"

    def test_run_id_filter(self, tmp_path):
        p = tmp_path / "traj.jsonl"
        lines = [
            json.dumps(_record("r1", "a", "success")),
            json.dumps(_record("r2", "b", "success")),
        ]
        p.write_text("\n".join(lines) + "\n")
        assert [s["node_name"] for s in steps_from_jsonl(p, run_id="r1")] == ["a"]
        assert [s["node_name"] for s in steps_from_jsonl(p, run_id="r2")] == ["b"]
        assert steps_from_jsonl(p, run_id="missing") == []

    def test_last_run_selected_when_no_run_id(self, tmp_path):
        p = tmp_path / "traj.jsonl"
        lines = [
            json.dumps(_record("r1", "a", "success")),
            json.dumps(_record("r2", "b", "success")),
            json.dumps(_record("r2", "c", "failed", error="boom")),
        ]
        p.write_text("\n".join(lines) + "\n")
        steps = steps_from_jsonl(p)
        assert [s["node_name"] for s in steps] == ["b", "c"]
        assert steps[1]["error"] == "boom"

    def test_bad_lines_and_missing_file(self, tmp_path):
        p = tmp_path / "traj.jsonl"
        p.write_text("not json\n{}\n" + json.dumps(_record("r1", "a", "success")) + "\n")
        assert [s["node_name"] for s in steps_from_jsonl(p)] == ["a"]
        assert steps_from_jsonl(tmp_path / "nope.jsonl") == []


class TestStepsFromSnapshot:
    def test_maps_snapshot_shape(self):
        snap = [{"node_name": "a", "status": "success", "error": "", "summary": "ok"}]
        steps = steps_from_snapshot(snap)
        assert steps == [
            {"node_name": "a", "node_id": "a", "upstream": [], "status": "success", "output_data": {}, "error": ""}
        ]
        assert steps_from_snapshot([]) == []


class TestCompileWorkflow:
    @pytest.mark.asyncio
    async def test_end_to_end_chain_and_condition(self):
        class Stub:
            async def chat(self, model, messages, temperature=0.0, max_tokens=0, **kw):
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "steps": [
                                {"node": "risk_probe"},
                                {"node": "sink", "when": {"var": "risk_probe.risk", "gte": 6}},
                            ]
                        }
                    )
                )

        steps = [
            {"node_name": "risk_probe", "status": "success", "output_data": {"risk": 8}, "error": ""},
            {"node_name": "sink", "status": "success", "output_data": {"done": True}, "error": ""},
        ]
        wf = Workflow(name="compiled")
        plan, node_ids = await compile_workflow(steps, Stub(), _make_factory(), wf)
        assert len(node_ids) == 2
        assert plan["steps"][1]["when"]["gte"] == 6
        sink_node = wf.nodes[node_ids[1]]
        assert sink_node.config.skip_if == [{"path": "risk", "negate": True, "gte": 6, "source": node_ids[0]}]
        edges = [(e.source_id, e.target_id) for e in wf.edges]
        assert edges == [(node_ids[0], node_ids[1])]

    @pytest.mark.asyncio
    async def test_empty_steps_no_crash(self):
        class Stub:
            async def chat(self, model, messages, **kw):
                raise AssertionError("LLM must not be called on empty steps")

        wf = Workflow(name="empty")
        plan, node_ids = await compile_workflow([], Stub(), _make_factory(), wf)
        assert plan == {"steps": []} and node_ids == []

    @pytest.mark.asyncio
    async def test_junk_reply_empty_plan(self):
        class Stub:
            async def chat(self, model, messages, **kw):
                return SimpleNamespace(content="no json here")

        steps = [{"node_name": "a", "status": "success", "output_data": {}, "error": ""}]
        wf = Workflow(name="junk")
        plan, node_ids = await compile_workflow(steps, Stub(), _make_factory(), wf)
        assert plan == {"steps": []} and node_ids == []
