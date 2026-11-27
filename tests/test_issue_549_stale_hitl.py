"""issue #549: terminal runs no longer retain hitl_pending/hitl_deadline written by mid-run GETs.

runs_get mutates the registry's shared record dict directly — HITL markers written while running
must be cleared once the run reaches a terminal state, otherwise success/failed runs keep
"hitl_pending: true" forever.
"""

from __future__ import annotations

import time

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node

HITL_NODE = "hitl_probe_549"


@register_node
class _ProbeNode(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe 549"
    category = NodeCategory.TOOL
    description = "test-only high-risk probe (#549)"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@pytest.fixture
def pm_no_guard(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_TIMEOUT", raising=False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {HITL_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def test_terminal_run_drops_stale_hitl_fields(pm_no_guard, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "test-key-549")
    from fusion_cowork.engine.workflow import WorkflowEngine

    eng = WorkflowEngine(permission_manager=pm_no_guard)
    app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
    auth = {"Authorization": "Bearer test-key-549"}
    wf_def = {
        "name": "hitl-wf-549",
        "workflow": {"nodes": [{"id": "n1", "name": HITL_NODE, "config": {}}], "edges": []},
    }
    _ProbeNode.calls = 0
    with TestClient(app) as client:
        wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
        run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
        for _ in range(200):
            if eng.hitl_pending(run_id):
                break
            time.sleep(0.05)
        else:
            pytest.fail("run never parked in HITL")
        # mid-run GET — writes hitl_pending/hitl_deadline/awaiting_human into the shared record
        detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
        assert detail["hitl_pending"] is True
        assert detail["hitl_deadline"] > time.time()
        assert detail["awaiting_human"] is True
        # Approve → run reaches terminal state
        action_id = client.get("/api/permissions/pending", headers=auth).json()["approvals"][0]["action_id"]
        assert (
            client.post(
                f"/api/permissions/pending/{action_id}/confirm",
                json={"approved": True, "once": True},
                headers=auth,
            ).status_code
            == 200
        )
        for _ in range(200):
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            if detail["status"] != "running":
                break
            time.sleep(0.05)
        assert detail["status"] == "success", detail
        # issue #549: terminal state retains no run-time HITL markers
        assert "hitl_pending" not in detail, detail
        assert "hitl_deadline" not in detail, detail
        assert "awaiting_human" not in detail, detail
        assert "awaiting_node" not in detail, detail
