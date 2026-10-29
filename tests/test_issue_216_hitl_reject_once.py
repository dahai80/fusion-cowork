"""issue #216: HITL 一次性拒绝 (once=true + approved=false) 必须唤醒暂停中的 run。

旧代码 released and approved 才唤醒 — 拒绝路径卡片出队后落入 confirm_guard,
条目已不在, confirmed=False, run 挂到 HITL 超时。拒绝也要 resolve_hitl(exec, False),
只是不落 once-grant、不落持久拒绝规则。
"""

from __future__ import annotations

import time

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import WorkflowEngine

HITL_NODE = "hitl_probe_216"
LOW_NODE = "low_probe_216"


@register_node
class _ProbeNode(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe 216"
    category = NodeCategory.TOOL
    description = "测试用: 高风险探针 (#216)"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@register_node
class _LowNode(BaseNode):
    name = LOW_NODE
    display_name = "Low Probe 216"
    category = NodeCategory.TOOL
    description = "测试用: 低风险探针"
    icon = "1️⃣"
    default_label = "Low"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"low": True}, summary="ok")


@pytest.fixture
def pm_no_guard(monkeypatch):
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


class TestRejectOnceWakesRun:
    def test_reject_once_wakes_run_without_rule(self, pm_no_guard, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-216")
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        auth = {"Authorization": "Bearer test-key-216"}
        _ProbeNode.calls = 0
        wf_def = {
            "name": "hitl-wf-216",
            "workflow": {
                "nodes": [
                    {"id": "n1", "name": LOW_NODE, "config": {}},
                    {"id": "n2", "name": HITL_NODE, "config": {}},
                ],
                "edges": [{"source_id": "n1", "target_id": "n2"}],
            },
        }
        with TestClient(app) as client:
            wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
            run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            hitl = False
            for _ in range(200):
                if eng.hitl_pending(run_id):
                    hitl = True
                    break
                time.sleep(0.05)
            assert hitl
            pending = client.get("/api/permissions/pending", headers=auth).json()["approvals"]
            action_id = pending[0]["action_id"]
            resp = client.post(
                f"/api/permissions/pending/{action_id}/confirm",
                json={"approved": False, "once": True},
                headers=auth,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["approved"] is False
            assert body["confirmed"] is True
            assert body["once"] is True
            # 卡片已出队
            assert client.get("/api/permissions/pending", headers=auth).json()["approvals"] == []
            # run 立即到达终态 (拒绝 + 无 continue_on_error → failed), 不等 1800s 超时
            detail = _wait_terminal(client, run_id, auth)
            assert detail["status"] == "failed", detail
            assert _ProbeNode.calls == 0
            # 一次性拒绝不落持久规则 — 同一工作流再跑仍暂停
            _ProbeNode.calls = 0
            run2 = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            hitl2 = False
            for _ in range(200):
                if eng.hitl_pending(run2):
                    hitl2 = True
                    break
                time.sleep(0.05)
            assert hitl2, "once 拒绝不应持久化规则"
            # 收尾 run2, 避免悬挂任务
            action2 = client.get("/api/permissions/pending", headers=auth).json()["approvals"][0]["action_id"]
            client.post(f"/api/permissions/pending/{action2}/confirm", json={"approved": False}, headers=auth)
            _wait_terminal(client, run2, auth)
