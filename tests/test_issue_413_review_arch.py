"""issue #413: intake architecture review gate (gate B) either-or — review_arch default false = auto-pass.

- approval_gate node require_review=false → engine skips permission/HITL, node outputs auto_passed
- require_review default true → still enters HITL as usual (gates A / C unaffected)
- product_lifecycle template: gate B wired to ${param.review_arch}, default assembly skips review
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
        # i18n: user-facing
        res = await _gate({"prompt": "闸门 B", "require_review": False}).execute({})
        assert res.status.value == "success"
        assert res.data["approved"] is True
        assert res.data["auto_passed"] is True
        assert res.data["review_arch"] is False

    async def test_require_review_absent_still_human_gate(self):
        # i18n: user-facing
        res = await _gate({"prompt": "闸门 A"}).execute({})
        assert res.status.value == "success"
        assert "auto_passed" not in res.data


class TestEngineSkip:
    async def test_auto_pass_gate_never_enters_hitl(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        eng = WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=True)
        wf = Workflow(name="wf-413")
        # i18n: user-facing
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
        # i18n: user-facing
        wf.add_node(_gate({"prompt": "闸门 B"}))
        task = asyncio.create_task(eng.execute(wf, execution_id="run_413_gate"))
        for _ in range(200):
            if eng.hitl_pending("run_413_gate"):
                break
            await asyncio.sleep(0.01)
        # i18n: user-facing
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
        # issue #387: gates A/C now wired to ${param.gates_human} (default true = human as usual) —
        # review_arch only controls gate B; gates A/C remain governed by their own parameter
        wf = self._tpl().template_to_workflow("product_lifecycle", param_values={"review_arch": True})
        gate_a = wf.nodes["n2"]
        gate_c = wf.nodes["n10"]
        assert gate_a.config.params["require_review"] is True
        assert gate_c.config.params["require_review"] is True
