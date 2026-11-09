"""issue #413: intake 架构评审闸门 (gate B) 二选一 — review_arch 缺省 false = 自动通过。

- approval_gate 节点 require_review=false → 引擎跳过权限/HITL, 节点输出 auto_passed
- require_review 缺省 true → 照常进 HITL (闸 A / 闸 C 不受影响)
- product_lifecycle 模板: 闸门 B 接 ${param.review_arch}, 缺省装配为免评审
"""

from __future__ import annotations

import asyncio

from fusion_cowork.engine.node import NodeRegistry
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.ops_nodes import ApprovalGateNode
from fusion_cowork.templates.template_manager import TemplateManager

import_all_nodes()


def _gate(params: dict | None = None) -> ApprovalGateNode:
    n = NodeRegistry.create("approval_gate", node_id="n_gate")
    if params:
        n.config.params = params
    return n


class TestGateNode:
    def test_schema_has_require_review_default_true(self):
        schema = ApprovalGateNode().get_params_schema()
        assert schema["properties"]["require_review"]["default"] is True

    async def test_require_review_false_auto_passes(self):
        res = await _gate({"prompt": "闸门 B", "require_review": False}).execute({})
        assert res.status.value == "success"
        assert res.data["approved"] is True
        assert res.data["auto_passed"] is True
        assert res.data["review_arch"] is False

    async def test_require_review_absent_still_human_gate(self):
        res = await _gate({"prompt": "闸门 A"}).execute({})
        assert res.status.value == "success"
        assert "auto_passed" not in res.data


class TestEngineSkip:
    async def test_auto_pass_gate_never_enters_hitl(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=True)
        wf = Workflow(name="wf-413")
        wf.add_node(_gate({"prompt": "闸门 B", "require_review": False}))
        task = asyncio.create_task(eng.execute(wf, execution_id="run_413_auto"))
        execution = await task
        assert execution.status.value == "success"
        assert execution.steps[-1].output_data.get("auto_passed") is True
        assert not eng.hitl_pending("run_413_auto")

    async def test_default_gate_still_pauses(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=True)
        wf = Workflow(name="wf-413b")
        wf.add_node(_gate({"prompt": "闸门 B"}))
        task = asyncio.create_task(eng.execute(wf, execution_id="run_413_gate"))
        for _ in range(200):
            if eng.hitl_pending("run_413_gate"):
                break
            await asyncio.sleep(0.01)
        assert eng.hitl_pending("run_413_gate"), "缺省闸门必须照常进 HITL"
        eng.resolve_hitl("run_413_gate", True, once=True)
        execution = await task
        assert execution.status.value == "success"


class TestTemplate:
    def _tpl(self) -> TemplateManager:
        return TemplateManager()

    def test_default_assembly_auto_passes_gate_b(self):
        wf = self._tpl().template_to_workflow("product_lifecycle")
        gate_b = wf.nodes["n4"]
        assert gate_b.config.params["require_review"] is False

    def test_review_arch_true_keeps_human_gate(self):
        wf = self._tpl().template_to_workflow("product_lifecycle", param_values={"review_arch": True})
        gate_b = wf.nodes["n4"]
        assert gate_b.config.params["require_review"] is True

    def test_other_gates_untouched(self):
        # issue #387: 闸 A/C 改接 ${param.gates_human} (缺省 true = 照常人工) —
        # review_arch 只控闸 B, 闸 A/C 仍由独立参数控制
        wf = self._tpl().template_to_workflow("product_lifecycle", param_values={"review_arch": True})
        gate_a = wf.nodes["n2"]
        gate_c = wf.nodes["n10"]
        assert gate_a.config.params["require_review"] is True
        assert gate_c.config.params["require_review"] is True
