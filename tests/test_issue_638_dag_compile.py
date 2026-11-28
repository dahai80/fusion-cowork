import json
import tempfile
from pathlib import Path

import pytest

from fusion_cowork.engine.hooks import HookEvent, HookManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.trajectory.compile import steps_from_jsonl, steps_from_snapshot
from fusion_cowork.trajectory.cond_distill import build_evidence, plan_to_workflow
from fusion_cowork.trajectory.recorder import TrajectoryRecorder


class MockDagNode(BaseNode):
    name = "mock_dag"
    display_name = "Mock DAG"
    category = NodeCategory.TOOL
    description = "issue #638 test node"
    icon = "x"
    default_label = "Mock DAG"

    async def execute(self, inputs):
        return NodeResult(status=NodeStatus.SUCCESS, data={"passed_count": 1})


def _make_node(node_type, inst_id=""):
    node = MockDagNode(node_id=inst_id)
    node.name = node_type
    return node


def _kernel_record(node_id, node_name, upstream=None, status="success", out=None, run_id="exec_dag"):
    return {
        "event": "post_node_execute",
        "run_id": run_id,
        "node_id": node_id,
        "status": status,
        "payload": {
            "legacy_event": "post_node_execute",
            "execution_id": "exec_dag",
            "node_name": node_name,
            "error": "",
            "data": {
                "node_id": node_id,
                "node_name": node_name,
                "upstream_node_ids": upstream or [],
                "result": {"status": "success", "data": out or {}},
            },
        },
    }


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@pytest.fixture
def tmp_trajectory_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def tmp_session_store():
    from fusion_cowork.engine.session import SessionStore

    with tempfile.TemporaryDirectory() as d:
        yield SessionStore(str(Path(d) / "sessions.db"))


class TestEngineHookUpstream:
    async def test_post_hook_payload_carries_upstream_ids(self):
        # issue #638: engine must include upstream_node_ids in the POST payload
        wf = Workflow(name="fanout")
        wf.add_node(MockDagNode(node_id="n1"))
        wf.add_node(MockDagNode(node_id="n2a"))
        wf.add_node(MockDagNode(node_id="n2b"))
        wf.connect("n1", "n2a")
        wf.connect("n1", "n2b")

        captured = {}

        async def spy(ctx):
            if ctx.data.get("node_id") == "n2b":
                captured.update(ctx.data)

        hm = HookManager()
        hm.register(HookEvent.POST_NODE_EXECUTE, spy)
        engine = WorkflowEngine(hook_manager=hm)
        execution = await engine.execute(wf)
        assert execution.status == WorkflowStatus.SUCCESS
        assert captured.get("upstream_node_ids") == ["n1"]
        assert captured.get("node_name") == "mock_dag"

    async def test_post_hook_payload_carries_input_bindings(self):
        # issue #638 follow-up: routed edges land in the POST payload so the
        # compile path can restore port routing deterministically
        wf = Workflow(name="routed")
        wf.add_node(MockDagNode(node_id="n1"))
        wf.add_node(MockDagNode(node_id="n2"))
        wf.connect("n1", "n2", source_output="passed", target_input="data")
        wf.connect("n1", "n2")  # default edge must NOT appear in bindings

        captured = {}

        async def spy(ctx):
            if ctx.data.get("node_id") == "n2":
                captured.update(ctx.data)

        hm = HookManager()
        hm.register(HookEvent.POST_NODE_EXECUTE, spy)
        engine = WorkflowEngine(hook_manager=hm)
        execution = await engine.execute(wf)
        assert execution.status == WorkflowStatus.SUCCESS
        assert captured.get("input_bindings") == {"data": ["n1", "passed"]}


class TestStepsFromJsonl:
    async def test_recovers_instance_id_and_upstream(self, tmp_path):
        # issue #638: node_id + upstream survive recording -> recovery
        path = tmp_path / "exec_dag.jsonl"
        _write_jsonl(
            path,
            [
                _kernel_record("filter_json", "filter", [], out={"passed_count": 2}),
                _kernel_record("filter_txt", "filter", [], out={"passed_count": 1}),
                _kernel_record("merge", "merge", ["filter_json", "filter_txt"]),
            ],
        )
        steps = steps_from_jsonl(path, run_id="exec_dag")
        assert [s["node_id"] for s in steps] == ["filter_json", "filter_txt", "merge"]
        assert steps[0]["node_name"] == "filter"
        assert steps[2]["upstream"] == ["filter_json", "filter_txt"]

    async def test_recovers_input_bindings(self, tmp_path):
        # issue #638 follow-up: routed ports survive recording -> recovery
        path = tmp_path / "exec_bind.jsonl"
        rec = _kernel_record("file_output", "file_output", ["filter_json"])
        rec["payload"]["data"]["input_bindings"] = {"data": ["filter_json", "passed"]}
        _write_jsonl(path, [rec])
        steps = steps_from_jsonl(path, run_id="exec_dag")
        assert steps[0]["input_bindings"] == {"data": ["filter_json", "passed"]}

    async def test_missing_upstream_defaults_empty(self, tmp_path):
        path = tmp_path / "exec_lin.jsonl"
        rec = _kernel_record("n1", "shell", run_id="exec_lin")
        rec["payload"]["data"].pop("upstream_node_ids")
        _write_jsonl(path, [rec])
        steps = steps_from_jsonl(path, run_id="exec_lin")
        assert steps[0]["upstream"] == []
        assert steps[0]["node_id"] == "n1"

    async def test_recorder_roundtrip_via_hooks(self, tmp_trajectory_dir, tmp_session_store):
        # real recorder path: fired hook payload -> JSONL -> steps_from_jsonl
        hm = HookManager()
        rec = TrajectoryRecorder(
            hm, session_store=tmp_session_store, session_id="s638", trajectory_dir=tmp_trajectory_dir
        )
        rec.attach()
        await hm.fire(HookEvent.WORKFLOW_START, {"execution_id": "exec_rt", "workflow_id": "wf", "workflow_name": "w"})
        await hm.fire(
            HookEvent.POST_NODE_EXECUTE,
            {
                "execution_id": "exec_rt",
                "node_id": "filter_txt",
                "node_name": "filter",
                "upstream_node_ids": ["filter_json"],
                "result": None,
                "status": "success",
            },
        )
        path = tmp_trajectory_dir / "s638.jsonl"
        assert path.is_file()
        steps = steps_from_jsonl(path, run_id="exec_rt")
        assert len(steps) == 1
        assert steps[0]["node_id"] == "filter_txt"
        assert steps[0]["upstream"] == ["filter_json"]


class TestStepsFromSnapshot:
    def test_snapshot_entries_carry_instance_fields(self):
        snap = [
            {"node_id": "filter_json", "node_name": "filter", "status": "success", "error": "", "summary": ""},
            {
                "node_id": "merge",
                "node_name": "merge",
                "status": "success",
                "error": "",
                "summary": "",
                "upstream": ["filter_json"],
            },
        ]
        steps = steps_from_snapshot(snap)
        assert steps[0]["node_id"] == "filter_json"
        assert steps[0]["upstream"] == []
        assert steps[1]["upstream"] == ["filter_json"]


class TestBuildEvidence:
    def test_evidence_includes_id_and_upstream(self):
        steps = [
            {
                "node_name": "filter",
                "node_id": "filter_json",
                "upstream": [],
                "status": "success",
                "output_data": {"passed_count": 2},
                "error": "",
            },
            {
                "node_name": "merge",
                "node_id": "merge",
                "upstream": ["filter_json", "filter_txt"],
                "status": "success",
                "output_data": {},
                "error": "",
            },
        ]
        ev = build_evidence(steps, run_success=True)
        assert ev[0]["id"] == "filter_json"
        assert ev[1]["upstream"] == ["filter_json", "filter_txt"]

    def test_evidence_step_objects(self):
        class S:
            node_id = "n1"
            node_name = "shell"
            upstream = ["n0"]
            status = "success"
            output_data = {}
            error = ""

        ev = build_evidence([S()], run_success=True)
        assert ev[0]["id"] == "n1"
        assert ev[0]["upstream"] == ["n0"]


class TestPlanToWorkflowDag:
    def test_fanout_fanin_edges_preserved(self):
        # issue #638: recorded DAG shape replays as DAG, not linear chain
        plan = {
            "steps": [
                {"node": "mock_dag", "id": "input", "upstream": []},
                {"node": "mock_dag", "id": "filter_json", "upstream": ["input"]},
                {"node": "mock_dag", "id": "filter_txt", "upstream": ["input"]},
                {"node": "mock_dag", "id": "merge", "upstream": ["filter_json", "filter_txt"]},
            ]
        }
        wf = Workflow(name="dag")
        plan_to_workflow(plan, lambda n: _make_node(n), wf)
        assert len(wf.nodes) == 4
        assert sorted(wf.get_upstream_nodes("merge")) == sorted(["filter_json", "filter_txt"])
        assert wf.get_upstream_nodes("filter_txt") == ["input"]
        assert wf.get_upstream_nodes("filter_json") == ["input"]

    def test_condition_source_resolves_by_instance_id(self):
        plan = {
            "steps": [
                {"node": "mock_dag", "id": "filter_json", "upstream": [], "when": None},
                {
                    "node": "mock_dag",
                    "id": "merge",
                    "upstream": ["filter_json"],
                    "when": {"var": "filter_json.passed_count", "gte": 1},
                },
            ]
        }
        wf = Workflow(name="cond")
        plan_to_workflow(plan, lambda n: _make_node(n), wf)
        assert wf.nodes["merge"].config.skip_if[0]["source"] == "filter_json"
        assert wf.nodes["merge"].config.skip_if[0]["gte"] == 1

    def test_condition_source_resolves_by_type_name(self):
        # LLM may key the condition var on the node type when ids are absent
        plan = {
            "steps": [
                {"node": "mock_dag", "id": "f1", "upstream": []},
                {
                    "node": "mock_dag",
                    "id": "merge",
                    "upstream": ["f1"],
                    "when": {"var": "mock_dag.passed_count", "gte": 1},
                },
            ]
        }
        wf = Workflow(name="cond2")
        plan_to_workflow(plan, lambda n: _make_node(n), wf)
        assert wf.nodes["merge"].config.skip_if[0]["source"] == "f1"

    def test_legacy_linear_plan_unchanged(self):
        # no id / upstream anywhere -> legacy linear chain fallback
        plan = {
            "steps": [
                {"node": "mock_dag", "when": None},
                {"node": "mock_dag", "when": {"var": "mock_dag.passed_count", "gte": 1}},
                {"node": "mock_dag", "when": None},
            ]
        }
        wf = Workflow(name="lin")
        ids = plan_to_workflow(plan, lambda n: _make_node(n), wf)
        assert len(ids) == 3
        assert wf.get_upstream_nodes(ids[1]) == [ids[0]]
        assert wf.get_upstream_nodes(ids[2]) == [ids[1]]

    def test_duplicate_instance_id_skipped(self):
        plan = {
            "steps": [
                {"node": "mock_dag", "id": "f1", "upstream": []},
                {"node": "mock_dag", "id": "f1", "upstream": ["f1"]},
            ]
        }
        wf = Workflow(name="dup")
        ids = plan_to_workflow(plan, lambda n: _make_node(n), wf)
        assert len(ids) == 1

    def test_upstream_outside_plan_falls_back_to_chain(self):
        plan = {
            "steps": [
                {"node": "mock_dag", "id": "input", "upstream": []},
                {"node": "mock_dag", "id": "f1", "upstream": ["ghost"]},
            ]
        }
        wf = Workflow(name="ghost")
        ids = plan_to_workflow(plan, lambda n: _make_node(n), wf)
        assert wf.get_upstream_nodes(ids[1]) == [ids[0]]


class TestWorkflowTopo:
    def test_dag_workflow_sorts_topologically(self):
        plan = {
            "steps": [
                {"node": "mock_dag", "id": "input", "upstream": []},
                {"node": "mock_dag", "id": "fj", "upstream": ["input"]},
                {"node": "mock_dag", "id": "ft", "upstream": ["input"]},
                {"node": "mock_dag", "id": "merge", "upstream": ["fj", "ft"]},
            ]
        }
        wf = Workflow(name="topo")
        plan_to_workflow(plan, lambda n: _make_node(n), wf)
        order = wf.topological_sort()
        assert order.index("merge") > order.index("fj")
        assert order.index("merge") > order.index("ft")
