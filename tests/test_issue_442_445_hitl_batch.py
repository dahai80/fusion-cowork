"""issues #442/#443/#444/#445 batch.

- #442: gate approval (non-once) must let the run proceed — since #340 gates persist
  no approve rule, an in-place recheck would always deny
- #445: pause/resume and HITL share the same event — resume waking without a decision
  = re-arm the wait, never synthesize a denial
- #444: base verdict blanks the raw matched field too (output_data is exposed verbatim
  through the runs API)
- #443: template n1 seat input carries the original requirement (task_title/task_prompt)
"""

from __future__ import annotations

import time

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import WorkflowEngine
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.templates.template_manager import TemplateManager

import_all_nodes()

LOW_NODE = "low_probe_442"


@register_node
class _LowNode(BaseNode):
    name = LOW_NODE
    display_name = "Low Probe 442"
    category = NodeCategory.TOOL
    description = "test probe node (low risk)"
    icon = "1️⃣"
    default_label = "Low"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"low": True}, summary="ok")


@pytest.fixture
def hitl_env(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_TIMEOUT", raising=False)
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _gate_workflow():
    return {
        "name": "hitl-wf-442",
        "workflow": {
            "nodes": [
                {"id": "g1", "name": "approval_gate", "config": {"params": {"prompt": "gate"}}},
                {"id": "n2", "name": LOW_NODE, "config": {}},
            ],
            "edges": [{"source_id": "g1", "target_id": "n2"}],
        },
    }


def _wait_pending(eng, run_id, timeout=10.0):
    for _ in range(int(timeout * 20)):
        if eng.hitl_pending(run_id):
            return True
        time.sleep(0.05)
    return False


def _wait_terminal(client, run_id, auth, timeout=10.0):
    detail = {}
    for _ in range(int(timeout * 20)):
        detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
        if detail.get("status") != "running":
            return detail
        time.sleep(0.05)
    return detail


def _make_app(eng, tmp_path, monkeypatch):

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-442")
    app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
    return app, {"Authorization": "Bearer test-key-442"}


class TestGateApproveWithoutOnce:
    # issue #442: non-once gate approval must let the run proceed (gates persist no rule; recheck would deny)

    def test_gate_approve_proceeds(self, hitl_env, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        eng = WorkflowEngine(permission_manager=hitl_env)
        app, auth = _make_app(eng, tmp_path, monkeypatch)
        _LowNode.calls = 0
        with TestClient(app) as client:
            wf_id = client.post("/api/workflows", json=_gate_workflow(), headers=auth).json()["id"]
            run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            assert _wait_pending(eng, run_id)
            action_id = client.get("/api/permissions/pending", headers=auth).json()["approvals"][0]["action_id"]
            resp = client.post(
                f"/api/permissions/pending/{action_id}/confirm",
                json={"approved": True},
                headers=auth,
            )
            assert resp.status_code == 200, resp.text
            detail = _wait_terminal(client, run_id, auth)
            assert detail["status"] == "success", detail
            assert _LowNode.calls == 1

    def test_gate_deny_still_fails(self, hitl_env, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        eng = WorkflowEngine(permission_manager=hitl_env)
        app, auth = _make_app(eng, tmp_path, monkeypatch)
        _LowNode.calls = 0
        with TestClient(app) as client:
            wf_id = client.post("/api/workflows", json=_gate_workflow(), headers=auth).json()["id"]
            run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            assert _wait_pending(eng, run_id)
            action_id = client.get("/api/permissions/pending", headers=auth).json()["approvals"][0]["action_id"]
            client.post(
                f"/api/permissions/pending/{action_id}/confirm",
                json={"approved": False},
                headers=auth,
            )
            detail = _wait_terminal(client, run_id, auth)
            assert detail["status"] == "failed", detail
            assert _LowNode.calls == 0


class TestResumeAtGateRearms:
    # issue #445: pause/resume passes through the HITL gate — after resume the gate waits again, no synthesized denial

    def test_resume_keeps_gate_pending_then_approve_completes(self, hitl_env, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        eng = WorkflowEngine(permission_manager=hitl_env)
        app, auth = _make_app(eng, tmp_path, monkeypatch)
        _LowNode.calls = 0
        with TestClient(app) as client:
            wf_id = client.post("/api/workflows", json=_gate_workflow(), headers=auth).json()["id"]
            run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            assert _wait_pending(eng, run_id)
            assert eng.pause(run_id) is True
            assert eng.resume(run_id) is True
            # resume wakes the run but no approval decision — gate must still wait
            # (old code synthesized a denial here, run failed instantly)
            time.sleep(0.4)
            assert eng.hitl_pending(run_id), "resume without decision must re-arm the HITL wait"
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            assert detail["status"] == "running", detail
            # Then the once approval arrives — wakes and completes
            action_id = client.get("/api/permissions/pending", headers=auth).json()["approvals"][0]["action_id"]
            client.post(
                f"/api/permissions/pending/{action_id}/confirm",
                json={"approved": True, "once": True},
                headers=auth,
            )
            detail = _wait_terminal(client, run_id, auth)
            assert detail["status"] == "success", detail
            assert _LowNode.calls == 1


class TestBaseVerdictHidesRawMatched:
    # issue #444: base verdict blanks the raw matched — runs API step output_data must not leak other customers' projects

    async def test_base_matched_empty(self):
        from fusion_cowork.nodes.ops.match_node import HistoricalMatchNode

        node = HistoricalMatchNode()
        node.config.params = {
            "requirement": "pet hotel booking with payments",
            "candidates": [
                {
                    "slug": "invoice-toolkit",
                    "repo": "business/ai-invoice-toolkit",
                    "capabilities": ["pet hotel", "booking", "payments"],
                    "customer": "tenant-a",
                }
            ],
            "own_customer": "tenant-b",
        }
        res = await node.execute({})
        assert res.data["verdict"] == "base"
        assert res.data["matched"] == {}
        assert res.data["matched_visible"] == {}
        assert res.data["matched_repo"] == ""

    async def test_extend_matched_kept(self):
        from fusion_cowork.nodes.ops.match_node import HistoricalMatchNode

        node = HistoricalMatchNode()
        node.config.params = {
            "requirement": "pet hotel booking with payments",
            "candidates": [
                {
                    "slug": "pet-hotel",
                    "repo": "o/pet-hotel",
                    "capabilities": ["pet hotel", "booking", "payments"],
                    "customer": "",
                }
            ],
        }
        res = await node.execute({})
        assert res.data["verdict"] == "extend"
        assert res.data["matched"]["repo"] == "o/pet-hotel"


class TestSeatInputCarriesRequirement:
    # issue #443: n1 seat input must carry task_title/task_prompt — with only a slug the BA would invent the PRD

    def test_template_n1_input_has_placeholders(self):
        tpl = TemplateManager().get_template("product_lifecycle")
        n1 = next(n for n in tpl["workflow"]["nodes"] if n["id"] == "n1")
        assert "${param.task_title}" in n1["config"]["params"]["input"]
        assert "${param.task_prompt}" in n1["config"]["params"]["input"]

    def test_assembled_n1_input_embeds_requirement(self):
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle",
            # i18n: user-facing
            {"slug": "tiny-todo", "repo": "o/r", "task_title": "实现极简待办清单 CLI", "task_prompt": "支持增删查"},
        )
        n1_input = wf.nodes["n1"].config.params["input"]
        assert "实现极简待办清单 CLI" in n1_input  # i18n: user-facing
        assert "支持增删查" in n1_input  # i18n: user-facing
