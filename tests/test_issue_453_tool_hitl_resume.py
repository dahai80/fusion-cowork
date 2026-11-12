"""issue #453: shell_exec HITL approve-once — 节点必须原地执行, run 不得谎报 success。

Repro: 高风险 tool 节点 HITL 暂停 → POST /api/permissions/pending/{id}/confirm
{"approved": true, "once": true} → 观察节点是否真的执行。
"""

from __future__ import annotations

import time

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import WorkflowEngine

HITL_NODE = "hitl_probe_453"


@register_node
class _ProbeNode(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe 453"
    category = NodeCategory.TOOL
    description = "test probe: high-risk tool node (#453)"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"executed": True, "stdout": "probe453done\n"}, summary="ran")


@pytest.fixture
def pm_hitl(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_TIMEOUT", raising=False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {HITL_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wait_terminal(client, run_id: str, auth: dict, timeout: float = 10.0):
    detail = {}
    for _ in range(int(timeout * 20)):
        detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
        if detail.get("status") != "running":
            return detail
        time.sleep(0.05)
    return detail


class TestToolHitlApproveExecutes:
    def test_approve_once_executes_node(self, pm_hitl, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-453")
        eng = WorkflowEngine(permission_manager=pm_hitl)
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        auth = {"Authorization": "Bearer test-key-453"}
        _ProbeNode.calls = 0
        wf_def = {
            "name": "hitl-wf-453",
            "workflow": {"nodes": [{"id": "n1", "name": HITL_NODE, "config": {}}], "edges": []},
        }
        with TestClient(app) as client:
            wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
            run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            for _ in range(200):
                if eng.hitl_pending(run_id):
                    break
                time.sleep(0.05)
            assert eng.hitl_pending(run_id)
            pending = client.get("/api/permissions/pending", headers=auth).json()["approvals"]
            action_id = pending[0]["action_id"]
            resp = client.post(
                f"/api/permissions/pending/{action_id}/confirm",
                json={"approved": True, "once": True},
                headers=auth,
            )
            assert resp.status_code == 200, resp.text
            detail = _wait_terminal(client, run_id, auth)
            steps = [s for s in detail.get("steps", []) if s.get("node_id") == "n1"]
            terminal = [s for s in steps if s.get("status") != "blocked"]
            assert _ProbeNode.calls == 1, f"node never executed: calls={_ProbeNode.calls} steps={steps}"
            assert detail["status"] == "success", detail
            assert terminal, f"no terminal step for n1: {steps}"
            assert terminal[-1]["status"] == "success", terminal[-1]

    def test_approve_with_persisted_rule_executes_node(self, pm_hitl, tmp_path, monkeypatch):
        # 非 once 批准 — confirm_guard 落规则后原地复查必须放行
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-453b")
        eng = WorkflowEngine(permission_manager=pm_hitl)
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        auth = {"Authorization": "Bearer test-key-453b"}
        _ProbeNode.calls = 0
        wf_def = {
            "name": "hitl-wf-453b",
            "workflow": {"nodes": [{"id": "n1", "name": HITL_NODE, "config": {}}], "edges": []},
        }
        with TestClient(app) as client:
            wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
            run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            for _ in range(200):
                if eng.hitl_pending(run_id):
                    break
                time.sleep(0.05)
            assert eng.hitl_pending(run_id)
            pending = client.get("/api/permissions/pending", headers=auth).json()["approvals"]
            action_id = pending[0]["action_id"]
            resp = client.post(
                f"/api/permissions/pending/{action_id}/confirm",
                json={"approved": True},
                headers=auth,
            )
            assert resp.status_code == 200, resp.text
            detail = _wait_terminal(client, run_id, auth)
            steps = [s for s in detail.get("steps", []) if s.get("node_id") == "n1"]
            assert _ProbeNode.calls == 1, f"node never executed: calls={_ProbeNode.calls} steps={steps}"
            assert detail["status"] == "success", detail


class _RaceHookManager:
    # 审批在引擎发 hitl_pending 通知期间早到 (resolve_hitl set 事件) —
    # 模拟真实 webhook/邮件回调快于引擎进入 evt.wait 的竞态窗口

    def __init__(self, engine: WorkflowEngine):
        self._engine = engine

    async def fire(self, event, payload):
        if getattr(event, "name", "") == "NOTIFICATION" and payload.get("kind") == "hitl_pending":
            exec_id = payload["execution_id"]
            self._engine.resolve_hitl(
                exec_id,
                True,
                once=True,
                attribution={"approval_id": "race-453", "decided_by": "race", "decided_at": "now"},
            )
        return None


class TestEarlyApprovalRace:
    def test_early_approval_during_hook_fire_not_lost(self, pm_hitl, tmp_path, monkeypatch):
        # issue #453: 早到批准曾被进入等待循环前的 evt.clear() 吞掉 —
        # run 挂到 HITL 超时被自动拒绝, 节点永不执行。修复后早到决定必须放行
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-453c")
        monkeypatch.setenv("FUSION_COWORK_HITL_TIMEOUT", "2")
        eng = WorkflowEngine(permission_manager=pm_hitl)
        eng._hook_manager = _RaceHookManager(eng)
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        auth = {"Authorization": "Bearer test-key-453c"}
        _ProbeNode.calls = 0
        wf_def = {
            "name": "hitl-wf-453c",
            "workflow": {"nodes": [{"id": "n1", "name": HITL_NODE, "config": {}}], "edges": []},
        }
        with TestClient(app) as client:
            wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
            run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            detail = _wait_terminal(client, run_id, auth, timeout=15.0)
            steps = [s for s in detail.get("steps", []) if s.get("node_id") == "n1"]
            assert _ProbeNode.calls == 1, f"early approval lost: calls={_ProbeNode.calls} steps={steps}"
            assert detail["status"] == "success", detail
            blocked = [s for s in steps if s.get("status") == "blocked"]
            assert (
                blocked
                and blocked[0].get("output_data", {}).get("hitl_attribution", {}).get("approval_id") == "race-453"
            ), blocked
