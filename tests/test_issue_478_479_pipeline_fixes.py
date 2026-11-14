"""issues #478/#479: delivery pipeline fixes from the 2026-11-14 software-factory E2E.

- #479: template param substitution renders booleans as strings — approval_gate
  with require_review="false" must auto-pass (coerced), not fall into HITL pause
- #478: ${step.X.result.artifacts.0|fallback} degrades gracefully when the seat
  produced no artifacts; product_lifecycle n12 references ${step.n6.result.number}
  explicitly instead of the ambient ${result.number} (which resolves against the
  nearest upstream issue_set_labels envelope and breaks)
"""

from __future__ import annotations

from fusion_cowork.engine.node import NodeRegistry, NodeStatus
from fusion_cowork.engine.permission_manager import PermissionManager
from fusion_cowork.engine.workflow import (
    Workflow,
    WorkflowEngine,
    _find_unresolved_refs,
    _interpolate_step_refs,
)
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.templates.template_manager import TemplateManager

import_all_nodes()


class TestRefFallback478:
    def test_step_ref_fullmatch_fallback_used_on_miss(self):
        passed = {"n1": {"result": {"artifacts": []}}}
        out = _interpolate_step_refs("${step.n1.result.artifacts.0|no-id}", passed, {})
        assert out == "no-id"

    def test_step_ref_fullmatch_hit_ignores_fallback(self):
        passed = {"n1": {"result": {"artifacts": ["art-1"]}}}
        out = _interpolate_step_refs("${step.n1.result.artifacts.0|no-id}", passed, {})
        assert out == "art-1"

    def test_step_ref_embedded_fallback(self):
        passed = {"n1": {"result": {"artifacts": []}}}
        out = _interpolate_step_refs("id=${step.n1.result.artifacts.0|none}; see report", passed, {})
        assert out == "id=none; see report"

    def test_step_ref_no_fallback_keeps_literal(self):
        passed = {"n1": {"result": {}}}
        out = _interpolate_step_refs("${step.n1.result.artifacts.0}", passed, {})
        assert out == "${step.n1.result.artifacts.0}"
        assert _find_unresolved_refs(out) == ["${step.n1.result.artifacts.0}"]

    def test_result_ref_fallback(self):
        out = _interpolate_step_refs("${result.missing_field|dflt-478}", {}, {})
        assert out == "dflt-478"
        out2 = _interpolate_step_refs("${result.present_field|dflt-478}", {}, {"result": {"present_field": 7}})
        assert out2 == 7

    def test_fallback_leaves_no_unresolved_ref(self):
        passed = {"n1": {"result": {"artifacts": []}}}
        params = {"input": "PRD id: ${step.n1.result.artifacts.0|(absent)}"}
        out = _interpolate_step_refs(params, passed, {})
        assert _find_unresolved_refs(out) == []
        assert out["input"] == "PRD id: (absent)"


class TestProductLifecycleTemplate478:
    def test_n12_issue_number_explicit_step_ref(self):
        wf = TemplateManager().template_to_workflow("product_lifecycle")
        assert wf is not None
        n12 = wf.nodes["n12"]
        assert n12.config.params["issue_number"] == "${step.n6.result.number}"

    def test_n7_n8_keep_shorthand(self):
        # 手术式修改: n7/n8 的上游就是 issue_create (信封有 number), 保持速记不动
        wf = TemplateManager().template_to_workflow("product_lifecycle")
        assert wf.nodes["n7"].config.params["issue_number"] == "${result.number}"
        assert wf.nodes["n8"].config.params["issue_number"] == "${result.number}"

    def test_n3_n9_artifact_refs_have_fallback(self):
        wf = TemplateManager().template_to_workflow("product_lifecycle")
        n3_input = wf.nodes["n3"].config.params["input"]
        n9_input = wf.nodes["n9"].config.params["input"]
        assert "${step.n1.result.artifacts.0|" in n3_input
        assert "${step.n1.result.artifacts.0|" in n9_input
        assert "${step.n3.result.artifacts.0|" in n9_input

    def test_rendered_template_resolves_with_empty_artifacts(self):
        # 端到端: replay-dev 座位 artifacts=[] 场景 — n3 参数插值后无残留引用
        wf = TemplateManager().template_to_workflow("product_lifecycle")
        ba_report = "PRD-CONTENT-478"
        passed = {
            "n1": {"result": {"artifacts": [], "last_result": {"report": ba_report}}},
            "n4d": {"design_section": ""},
            "n4m": {"verdict": "new", "matched_repo": "", "extension_label": "", "extension_contract": ""},
        }
        n3_input = wf.nodes["n3"].config.params["input"]
        resolved = _interpolate_step_refs(n3_input, passed, passed.get("n4d", {}))
        assert "PRD-CONTENT-478" in resolved
        assert "${" not in resolved
        assert "(no artifact id" in resolved


class TestGateStringCoercion479:
    def _engine(self, hitl: bool) -> WorkflowEngine:
        return WorkflowEngine(permission_manager=PermissionManager(), hitl_pause=hitl)

    async def test_string_false_auto_passes(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        eng = self._engine(True)
        wf = Workflow(name="wf-479")
        gate = NodeRegistry.create("approval_gate", node_id="n_gate")
        gate.config.params = {"prompt": "闸门 A", "require_review": "false"}
        wf.add_node(gate)
        ex = await eng.execute(wf, execution_id="run_479_strfalse")
        assert ex.status.value == "success"
        assert ex.steps[-1].output_data.get("auto_passed") is True
        assert not eng.hitl_pending("run_479_strfalse")

    async def test_string_zero_and_off_auto_pass(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        for i, val in enumerate(("0", "off", "no")):
            eng = self._engine(True)
            wf = Workflow(name=f"wf-479-{i}")
            gate = NodeRegistry.create("approval_gate", node_id="n_gate")
            gate.config.params = {"prompt": "闸门", "require_review": val}
            wf.add_node(gate)
            ex = await eng.execute(wf, execution_id=f"run_479_{i}")
            assert ex.status.value == "success", f"require_review={val!r} should auto-pass"
            assert ex.steps[-1].output_data.get("auto_passed") is True

    async def test_string_true_still_gates(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        eng = self._engine(False)
        wf = Workflow(name="wf-479-true")
        gate = NodeRegistry.create("approval_gate", node_id="n_gate")
        gate.config.params = {"prompt": "闸门 A", "require_review": "true"}
        wf.add_node(gate)
        ex = await eng.execute(wf, execution_id="run_479_strtrue")
        # headless (hitl_pause=False): 字符串 "true" 不该误判为免评审 — 照常权限拒绝
        assert ex.status.value == "failed"
        assert ex.steps[-1].status == NodeStatus.DENIED

    async def test_bool_false_still_auto_passes(self, monkeypatch):
        # 回归锚: #413 原 boolean 行为不因 #479 强转改变
        monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        eng = self._engine(True)
        wf = Workflow(name="wf-479-bool")
        gate = NodeRegistry.create("approval_gate", node_id="n_gate")
        gate.config.params = {"prompt": "闸门 B", "require_review": False}
        wf.add_node(gate)
        ex = await eng.execute(wf, execution_id="run_479_boolfalse")
        assert ex.status.value == "success"
        assert ex.steps[-1].output_data.get("auto_passed") is True

    async def test_gate_node_accepts_string_false(self):
        # 节点自身分支 (ops_nodes) 早接受字符串形态 — 引擎对齐后两端一致
        from fusion_cowork.engine.node import NodeConfig

        n = NodeRegistry.create("approval_gate", config=NodeConfig(params={"prompt": "g", "require_review": "false"}))
        res = await n.execute({})
        assert res.status.value == "success"
        assert res.data["auto_passed"] is True
