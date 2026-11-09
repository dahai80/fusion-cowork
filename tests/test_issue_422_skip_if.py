"""issue #422: 历史项目比对 (match 节点) 基础原语 — 数据驱动条件跳过 skip_if。

verdict 分支路由需要引擎支持: 上游节点输出 (如 match verdict=extend) 命中
node.config.skip_if 全部条件时, 本节点记 SKIPPED 不执行不进权限/HITL。
"""

from __future__ import annotations

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, _resolve_data_path, _skip_if_hit


@register_node
class _EmitNode(BaseNode):
    name = "_t422_emit"
    display_name = "emit"
    category = NodeCategory.LOGIC

    def get_params_schema(self):
        return {"type": "object", "properties": {"verdict": {"type": "string"}, "score": {"type": "number"}}}

    async def execute(self, inputs):
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"verdict": str(self.config.params.get("verdict", "new")), "score": 0.87, "gaps": ["pay"]},
            summary="emitted",
        )


@register_node
class _BoomNode(BaseNode):
    name = "_t422_boom"
    display_name = "boom"
    category = NodeCategory.LOGIC

    async def execute(self, inputs):
        raise AssertionError("must not execute when skip_if hits")


def _wf(verdict: str, skip_cfg: dict | list) -> Workflow:
    wf = Workflow(name="t422")
    a = NodeRegistry.create("_t422_emit", node_id="m1")
    a.config.params = {"verdict": verdict}
    b = NodeRegistry.create("_t422_boom", node_id="down1")
    b.config.skip_if = skip_cfg
    wf.add_node(a)
    wf.add_node(b)
    wf.connect("m1", "down1")
    return wf


class TestSkipIfPrimitive:
    def test_resolve_data_path(self):
        assert _resolve_data_path({"a": {"b": 1}}, "a.b") == 1
        assert _resolve_data_path({"a": 1}, "a.b.c") is None
        assert _resolve_data_path({}, "") is None

    def test_skip_if_hit_descriptions(self):
        assert _skip_if_hit([{"path": "verdict", "equals": "new"}], {}, {"verdict": "new"})
        assert not _skip_if_hit([{"path": "verdict", "equals": "new"}], {}, {"verdict": "extend"})
        assert _skip_if_hit([{"path": "verdict", "in": ["extend", "base"]}], {}, {"verdict": "base"})
        assert not _skip_if_hit([{"path": "verdict", "in": ["extend"]}], {}, {"verdict": "new"})
        assert _skip_if_hit([{"path": "x", "not_in": ["new"]}], {}, {})
        # source 指定上游节点输出; 上游缺输出 = 条件不中 (fail-closed, 不误跳)
        assert not _skip_if_hit([{"source": "m1", "path": "verdict", "equals": "new"}], {}, {})
        assert _skip_if_hit([{"source": "m1", "path": "verdict", "equals": "new"}], {"m1": {"verdict": "new"}}, {})
        # 多条件须全中
        assert not _skip_if_hit(
            [{"path": "verdict", "equals": "new"}, {"path": "score", "equals": 0.9}],
            {},
            {"verdict": "new", "score": 0.87},
        )

    async def test_engine_skips_on_verdict(self):
        engine = WorkflowEngine()
        res = await engine.execute(_wf("new", [{"source": "m1", "path": "verdict", "equals": "new"}]))
        steps = {s.node_id: s for s in res.steps}
        assert steps["m1"].status.value == "success"
        assert steps["down1"].status.value == "skipped"
        assert "skip_if" in (steps["down1"].summary or "")

    async def test_engine_runs_when_condition_misses(self):
        engine = WorkflowEngine()
        wf = _wf("extend", [{"source": "m1", "path": "verdict", "equals": "new"}])
        res = await engine.execute(wf)
        steps = {s.node_id: s for s in res.steps}
        assert steps["down1"].status.value == "failed"  # condition missed, executed and raised

    async def test_skip_if_survives_roundtrip(self):
        wf = _wf("new", [{"source": "m1", "path": "verdict", "equals": "new"}])
        wf2 = Workflow.from_dict(wf.to_dict())
        assert wf2.nodes["down1"].config.skip_if == [{"source": "m1", "path": "verdict", "equals": "new"}]

    async def test_skipped_node_not_permission_gated(self):
        # skip 在权限检查之前 — 高风险节点命中 skip_if 不产生 HITL 暂停
        from fusion_cowork.engine.permission_manager import PermissionManager

        engine = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=True)
        wf = _wf("new", [{"source": "m1", "path": "verdict", "equals": "new"}])
        wf.nodes["down1"].name = "shell_exec"  # high-risk
        res = await engine.execute(wf)
        steps = {s.node_id: s for s in res.steps}
        assert steps["down1"].status.value == "skipped"
        assert res.status.value == "success"
