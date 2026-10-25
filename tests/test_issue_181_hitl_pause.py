"""issue #181: HITL — 高风险节点本地待审批时引擎暂停在节点边界, confirm 后原地续跑 (只重跑被闸节点)。"""

from __future__ import annotations

import asyncio
import time

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine

HITL_NODE = "hitl_probe_181"


@register_node
class _ProbeNode(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe"
    category = NodeCategory.TOOL
    description = "测试用: 高风险探针"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@register_node
class _PreNode(BaseNode):
    name = "hitl_pre_181"
    display_name = "HITL Pre"
    category = NodeCategory.TOOL
    description = "测试用: 闸前节点 (验证不被重跑)"
    icon = "1️⃣"
    default_label = "Pre"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"pre": True}, summary="ok")


@pytest.fixture
def pm_no_guard(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    from fusion_cowork.security import guard as guard_mod

    monkeypatch.setattr(guard_mod, "guard_enabled", lambda: False)
    # 探针节点不在静态 HIGH_RISK_NODES 里 — 动态并入, 避免与真实 shell_exec 注册冲突
    from fusion_cowork.engine import _permission_local

    monkeypatch.setattr(_permission_local, "HIGH_RISK_NODES", _permission_local.HIGH_RISK_NODES | {HITL_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf():
    wf = Workflow(name="hitl-wf-181")
    pre = NodeRegistry.create("hitl_pre_181")
    probe = NodeRegistry.create(HITL_NODE)
    pre_id = wf.add_node(pre)
    probe_id = wf.add_node(probe)
    wf.connect(pre_id, probe_id)
    return wf


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0):
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


class TestHitlPause:
    async def test_default_off_deny_fast(self, pm_no_guard):
        _ProbeNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        execution = await eng.execute(_wf(), execution_id="run-181-off")
        assert execution.status.value == "failed"
        assert _ProbeNode.calls == 0
        assert engine_denied_step(execution)

    async def test_pause_then_approve_resumes_in_place(self, pm_no_guard):
        _ProbeNode.calls = 0
        _PreNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard, hitl_pause=True)
        exec_id = "run-181-approve"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id), "run 未进入 HITL 等待"
        # 待审批项在队列中, 携带 execution_id
        pending = pm_no_guard.list_pending()
        assert len(pending) == 1
        assert pending[0]["execution_id"] == exec_id
        action_id = pending[0]["action_id"]
        assert await pm_no_guard.confirm_guard(action_id, approved=True, approved_by="test")
        assert eng.resolve_hitl(exec_id, True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"
        assert _ProbeNode.calls == 1
        # 闸前节点只跑过一次 — 原地续跑, 不重放整个 run
        assert _PreNode.calls == 1

    async def test_pause_then_reject_fails_run(self, pm_no_guard):
        _ProbeNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard, hitl_pause=True)
        exec_id = "run-181-reject"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        action_id = pm_no_guard.list_pending()[0]["action_id"]
        assert await pm_no_guard.confirm_guard(action_id, approved=False, approved_by="test")
        assert eng.resolve_hitl(exec_id, False)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "failed"
        assert _ProbeNode.calls == 0
        assert engine_denied_step(execution)

    async def test_continue_on_error_skips_hitl(self, pm_no_guard):
        _ProbeNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard, hitl_pause=True)
        wf = _wf()
        for n in wf.nodes.values():
            if n.name == HITL_NODE:
                n.config.continue_on_error = True
        exec_id = "run-181-coe"
        execution = await eng.execute(wf, execution_id=exec_id)
        # continue_on_error 节点不进 HITL 等待, 走原 deny-fast 跳过路径
        assert engine_denied_step(execution)
        assert _ProbeNode.calls == 0
        assert not eng.hitl_pending(exec_id)

    async def test_env_flag_enables_hitl(self, pm_no_guard, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_HITL_PAUSE", "1")
        _ProbeNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        assert eng._hitl_pause is True
        exec_id = "run-181-env"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        action_id = pm_no_guard.list_pending()[0]["action_id"]
        await pm_no_guard.confirm_guard(action_id, approved=True, approved_by="test")
        eng.resolve_hitl(exec_id, True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status.value == "success"


def engine_denied_step(execution) -> bool:
    return any(s.status == NodeStatus.DENIED for s in execution.steps)


class TestWebWiring:
    def test_confirm_route_wakes_hitl_run(self, pm_no_guard, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        static_key = "test-key-181"
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", static_key)
        _ProbeNode.calls = 0
        _PreNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard, hitl_pause=True)
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        auth = {"Authorization": f"Bearer {static_key}"}
        wf_def = {
            "name": "hitl-wf-181",
            "workflow": {
                "nodes": [
                    {"id": "n1", "name": "hitl_pre_181", "config": {}},
                    {"id": "n2", "name": HITL_NODE, "config": {}},
                ],
                "edges": [{"source_id": "n1", "target_id": "n2"}],
            },
        }
        with TestClient(app) as client:
            create_resp = client.post("/api/workflows", json=wf_def, headers=auth)
            assert create_resp.status_code in (200, 201), create_resp.text
            wf_id = create_resp.json()["id"]

            run_resp = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth)
            assert run_resp.status_code == 202, run_resp.text
            run_id = run_resp.json()["run_id"]

            # 等待 run 进入 HITL 等待点 (后台任务跑在 TestClient portal 事件循环上)
            hitl = False
            for _ in range(200):
                if eng.hitl_pending(run_id):
                    hitl = True
                    break
                time.sleep(0.05)
            assert hitl, "run 未进入 HITL 等待"

            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            assert detail.get("hitl_pending") is True

            pending = client.get("/api/permissions/pending", headers=auth).json()["approvals"]
            assert len(pending) == 1
            assert pending[0]["execution_id"] == run_id
            action_id = pending[0]["action_id"]

            confirm = client.post(
                f"/api/permissions/pending/{action_id}/confirm", json={"approved": True}, headers=auth
            )
            assert confirm.status_code == 200, confirm.text
            assert confirm.json()["confirmed"] is True

            for _ in range(200):
                detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
                if detail["status"] != "running":
                    break
                time.sleep(0.05)
            assert detail["status"] == "success", detail
            assert _ProbeNode.calls == 1
            assert _PreNode.calls == 1
