"""issue #467 (phase 3.2/3.3): coordinator-subagent 扇出 + session fork。

覆盖: coordinator_fanout (harness SubagentRunner 并行/顺序/失败传播/partial/
harness 缺失响亮报错)、session_fork (父 session 派生分支 id/汇聚/失败传播/
波边界经真实引擎)、HIGH_RISK 门禁、中文别名。
"""

from __future__ import annotations

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.permission_manager import HIGH_RISK_NODES
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

PROBE_OK = "fork_probe_ok_467"
PROBE_FAIL = "fork_probe_fail_467"


@register_node
class _ProbeOk(BaseNode):
    name = PROBE_OK
    display_name = "Fork Probe OK 467"
    category = NodeCategory.LOGIC
    description = "测试用: 成功探针 (#467)"
    icon = "🧪"
    default_label = "ProbeOk"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"sid": inputs.get("session_id", ""), "parent": inputs.get("parent_session", "")},
            summary=f"ok sid={inputs.get('session_id', '')}",
        )


@register_node
class _ProbeFail(BaseNode):
    name = PROBE_FAIL
    display_name = "Fork Probe Fail 467"
    category = NodeCategory.LOGIC
    description = "测试用: 失败探针 (#467)"
    icon = "🧪"
    default_label = "ProbeFail"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.FAILED, error="probe 467 intentional failure")


@pytest.fixture(autouse=True)
def _probe_nodes():
    for probe in (PROBE_OK, PROBE_FAIL):
        if NodeRegistry.get(probe) is None:
            import fusion_cowork.nodes.ecosystem.fanout_node  # noqa: F401
    yield


def _make(name: str, params: dict) -> BaseNode:
    node_cls = NodeRegistry.get(name)
    return node_cls(config=NodeConfig(params=params))


class TestCoordinatorFanout:
    async def test_parallel_default_workers_ack(self):
        node = _make("coordinator_fanout", {"workers": [{"name": "a", "prompt": "p1"}, {"name": "b", "prompt": "p2"}]})
        res = await node.execute({})
        assert res.status.value == "success"
        assert res.data["total"] == 2
        assert res.data["failed"] == 0
        sids = [r["subagent_id"] for r in res.data["results"]]
        assert len(set(sids)) == 2
        assert all(r["status"] == "completed" for r in res.data["results"])
        assert "acknowledged" in res.data["results"][0]["summary"]

    async def test_worker_node_execution(self):
        node = _make(
            "coordinator_fanout",
            {
                "workers": [
                    {"name": "w1", "prompt": "go", "worker_node": PROBE_OK, "worker_params": {"k": "v"}},
                ]
            },
        )
        res = await node.execute({})
        assert res.status.value == "success"
        r = res.data["results"][0]
        assert r["status"] == "completed"
        assert r["summary"].startswith("ok sid=subagent-")
        assert r["tokens_spent"] >= 0

    async def test_worker_node_missing_503_style_failure(self):
        node = _make("coordinator_fanout", {"workers": [{"name": "w", "prompt": "p", "worker_node": "nope_467"}]})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "nope_467" in res.data["results"][0]["error"]

    async def test_worker_failure_propagates(self):
        node = _make("coordinator_fanout", {"workers": [{"name": "w", "prompt": "p", "worker_node": PROBE_FAIL}]})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert res.data["failed"] == 1
        assert "w" in res.error

    async def test_allow_partial_success(self):
        node = _make(
            "coordinator_fanout",
            {
                "workers": [
                    {"name": "bad", "prompt": "p", "worker_node": PROBE_FAIL},
                    {"name": "good", "prompt": "p", "worker_node": PROBE_OK},
                ],
                "allow_partial": True,
            },
        )
        res = await node.execute({})
        assert res.status.value == "success"
        assert res.data["failed"] == 1

    async def test_sequential_mode(self):
        node = _make(
            "coordinator_fanout",
            {"workers": [{"name": "a", "prompt": "p"}, {"name": "b", "prompt": "p"}], "sequential": True},
        )
        res = await node.execute({})
        assert res.status.value == "success"

    async def test_missing_workers_param(self):
        node = _make("coordinator_fanout", {})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "workers" in res.error

    async def test_harness_missing_loud_error(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _no_harness(name, *a, **kw):
            if name == "fusion_harness" or name.startswith("fusion_harness."):
                raise ImportError("No module named 'fusion_harness'")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_harness)
        monkeypatch.setitem(__import__("sys").modules, "fusion_harness", None)
        node = _make("coordinator_fanout", {"workers": [{"name": "a", "prompt": "p"}]})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "fusion-harness" in res.error

    def test_high_risk_gate(self):
        assert "coordinator_fanout" in HIGH_RISK_NODES


class TestSessionFork:
    async def test_fork_branch_session_ids(self):
        from fusion_cowork.observability.trace import _trace_var

        token = _trace_var.set("run-fork-1")
        try:
            node = _make(
                "session_fork",
                {"branches": [{"name": "left", "node": PROBE_OK}, {"name": "right", "node": PROBE_OK}]},
            )
            res = await node.execute({})
        finally:
            _trace_var.reset(token)
        assert res.status.value == "success"
        assert res.data["parent_session"] == "run-fork-1"
        assert res.data["gathered"] is True
        b = res.data["branches"]
        assert b["left"]["session_id"] == "run-fork-1:fork:left"
        assert b["right"]["session_id"] == "run-fork-1:fork:right"
        assert all(v["status"] == "success" for v in b.values())

    async def test_fork_branch_failure_propagates(self):
        node = _make("session_fork", {"branches": [{"name": "x", "node": PROBE_FAIL}]})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "x" in res.error
        assert res.data["branches"]["x"]["error"] == "probe 467 intentional failure"

    async def test_fork_allow_partial(self):
        node = _make(
            "session_fork",
            {
                "branches": [{"name": "x", "node": PROBE_FAIL}, {"name": "y", "node": PROBE_OK}],
                "allow_partial": True,
            },
        )
        res = await node.execute({})
        assert res.status.value == "success"
        assert res.data["gathered"] is True

    async def test_fork_unknown_node(self):
        node = _make("session_fork", {"branches": [{"name": "z", "node": "missing_467"}]})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "missing_467" in res.data["branches"]["z"]["error"]

    async def test_fork_missing_branches(self):
        node = _make("session_fork", {})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "branches" in res.error

    def test_high_risk_gate(self):
        assert "session_fork" in HIGH_RISK_NODES

    async def test_engine_end_to_end(self):
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        pm.approve("session_fork")
        pm.approve(PROBE_OK)
        eng = WorkflowEngine(permission_manager=pm)
        wf = Workflow.from_dict(
            {
                "name": "wf-467",
                "nodes": [
                    {
                        "id": "fork",
                        "name": "session_fork",
                        "config": {"params": {"branches": [{"name": "b1", "node": PROBE_OK}]}},
                    }
                ],
                "edges": [],
            }
        )
        execution = await eng.execute(wf)
        assert execution.status.value == "success"
        out = execution.steps[0].output_data or {}
        assert out["gathered"] is True
        assert out["parent_session"] == execution.id
        assert out["branches"]["b1"]["session_id"] == f"{execution.id}:fork:b1"


def test_chinese_aliases_registered():
    from fusion_cowork import NODE_NAME_ALIASES

    assert NODE_NAME_ALIASES["协调分发"] == "coordinator_fanout"
    assert NODE_NAME_ALIASES["子代理分发"] == "coordinator_fanout"
    assert NODE_NAME_ALIASES["会话分叉"] == "session_fork"
    assert NODE_NAME_ALIASES["会话分叉并行"] == "session_fork"
