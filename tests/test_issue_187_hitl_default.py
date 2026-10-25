"""issue #187: HITL 默认开启 + 有界超时自动拒绝 + completed_with_denials 终态 + 一次性批准不落规则。"""

from __future__ import annotations

import asyncio
import time

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus, _parse_hitl_timeout

HITL_NODE = "hitl_probe_187"
LOW_NODE = "low_probe_187"


@register_node
class _ProbeNode(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe 187"
    category = NodeCategory.TOOL
    description = "测试用: 高风险探针 (#187)"
    icon = "🧪"
    default_label = "Probe"

    calls = 0

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@register_node
class _LowNode(BaseNode):
    name = LOW_NODE
    display_name = "Low Probe 187"
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
    from fusion_cowork.engine import _permission_local

    monkeypatch.setattr(_permission_local, "HIGH_RISK_NODES", _permission_local.HIGH_RISK_NODES | {HITL_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf():
    wf = Workflow(name="hitl-wf-187")
    low = NodeRegistry.create(LOW_NODE)
    probe = NodeRegistry.create(HITL_NODE)
    low_id = wf.add_node(low)
    probe_id = wf.add_node(probe)
    wf.connect(low_id, probe_id)
    return wf


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0):
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


def _denied(execution) -> bool:
    return any(s.status == NodeStatus.DENIED for s in execution.steps)


class TestHitlDefaults:
    # issue #187: FUSION_COWORK_HITL_PAUSE 语义翻转 — 默认 ON, 显式 off 值才关闭
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, 1800.0),
            ("", 1800.0),
            ("1", 1.0),
            ("1.5", 1.5),
            ("30s", 30.0),
            ("5m", 300.0),
            ("2h", 7200.0),
            ("garbage", 1800.0),
        ],
    )
    def test_parse_hitl_timeout(self, raw, expected):
        assert _parse_hitl_timeout(raw) == expected

    def test_default_on(self, pm_no_guard):
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        assert eng._hitl_pause is True
        assert eng._hitl_timeout == 1800.0

    @pytest.mark.parametrize("raw", ["0", "false", "off", "no", "OFF", "False"])
    def test_env_off_values(self, pm_no_guard, monkeypatch, raw):
        monkeypatch.setenv("FUSION_COWORK_HITL_PAUSE", raw)
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        assert eng._hitl_pause is False

    @pytest.mark.parametrize("raw", ["1", "true", "on", "yes"])
    def test_env_on_values(self, pm_no_guard, monkeypatch, raw):
        monkeypatch.setenv("FUSION_COWORK_HITL_PAUSE", raw)
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        assert eng._hitl_pause is True

    def test_env_timeout(self, pm_no_guard, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_HITL_TIMEOUT", "90s")
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        assert eng._hitl_timeout == 90.0


class TestHitlFlow:
    async def test_timeout_auto_denies_and_cleans_queue(self, pm_no_guard, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_HITL_TIMEOUT", "1")
        _ProbeNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        exec_id = "run-187-timeout"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        deadline = eng.hitl_deadline(exec_id)
        assert deadline is not None and deadline > time.time()
        execution = await asyncio.wait_for(task, timeout=5)
        # 超时 → 自动拒绝 → 非 continue_on_error 节点 → run 失败
        assert execution.status == WorkflowStatus.FAILED
        assert _denied(execution)
        assert _ProbeNode.calls == 0
        # 滞留待审批项被清理, 截止时间被弹出
        assert pm_no_guard.list_pending() == []
        assert eng.hitl_deadline(exec_id) is None
        assert not eng.hitl_pending(exec_id)

    async def test_completed_with_denials_terminal(self, pm_no_guard):
        _ProbeNode.calls = 0
        _LowNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        wf = _wf()
        for n in wf.nodes.values():
            if n.name == HITL_NODE:
                n.config.continue_on_error = True
        execution = await eng.execute(wf, execution_id="run-187-cwd")
        # continue_on_error 高风险节点不进 HITL, deny-fast 跳过; run 完成但带否决
        assert execution.status == WorkflowStatus.COMPLETED_WITH_DENIALS
        assert execution.status.value == "completed_with_denials"
        assert _denied(execution)
        assert _ProbeNode.calls == 0
        assert _LowNode.calls == 1

    async def test_non_high_risk_unaffected(self, pm_no_guard):
        _LowNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        wf = Workflow(name="hitl-wf-187-low")
        node = NodeRegistry.create(LOW_NODE)
        wf.add_node(node)
        execution = await eng.execute(wf, execution_id="run-187-low")
        assert execution.status == WorkflowStatus.SUCCESS
        assert _LowNode.calls == 1

    async def test_release_pending_once_no_rule_persist(self, pm_no_guard):
        _ProbeNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        # run1: 暂停 → 一次性批准 (不落规则) → 原地续跑成功
        exec1 = "run-187-once-1"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec1))
        assert await _wait_hitl(eng, exec1)
        pending = pm_no_guard.list_pending()
        assert len(pending) == 1
        action_id = pending[0]["action_id"]
        assert pm_no_guard.release_pending_once(action_id) is True
        assert pm_no_guard.list_pending() == []
        assert pm_no_guard.release_pending_once(action_id) is False
        assert eng.resolve_hitl(exec1, True, once=True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.SUCCESS
        assert _ProbeNode.calls == 1
        # run2: 同一 PermissionManager, 规则未持久化 → 再次暂停
        _ProbeNode.calls = 0
        exec2 = "run-187-once-2"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec2))
        assert await _wait_hitl(eng, exec2), "一次性批准不应留下持久规则"
        # 收尾: confirm_guard 落规则后唤醒
        action_id2 = pm_no_guard.list_pending()[0]["action_id"]
        await pm_no_guard.confirm_guard(action_id2, approved=True, approved_by="test")
        eng.resolve_hitl(exec2, True)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.SUCCESS
        # run3: 规则已持久化 → 不再暂停
        _ProbeNode.calls = 0
        execution = await eng.execute(_wf(), execution_id="run-187-once-3")
        assert execution.status == WorkflowStatus.SUCCESS
        assert _ProbeNode.calls == 1

    async def test_discard_pending_by_execution(self, pm_no_guard):
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        exec_id = "run-187-discard"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        action_id = pm_no_guard.list_pending()[0]["action_id"]
        removed = pm_no_guard.discard_pending_by_execution(exec_id)
        assert removed == [action_id]
        assert pm_no_guard.list_pending() == []
        assert pm_no_guard.discard_pending_by_execution(exec_id) == []
        eng.resolve_hitl(exec_id, False)
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.FAILED


class TestWebWiring:
    def _app_ctx(self, pm_no_guard, tmp_path, monkeypatch, static_key="test-key-187"):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", static_key)
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        return TestClient, app, eng

    def test_confirm_once_wakes_run_without_rule(self, pm_no_guard, tmp_path, monkeypatch):
        TestClient, app, eng = self._app_ctx(pm_no_guard, tmp_path, monkeypatch)
        _ProbeNode.calls = 0
        auth = {"Authorization": "Bearer test-key-187"}
        wf_def = {
            "name": "hitl-wf-187",
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
            # run 详情带 hitl_pending + hitl_deadline (默认 1800s)
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            assert detail["hitl_pending"] is True
            assert detail["hitl_deadline"] > time.time() + 1700
            # 刷新页面语义: pending 队列仍在, 页面重载可恢复审批卡
            pending = client.get("/api/permissions/pending", headers=auth).json()["approvals"]
            assert len(pending) == 1 and pending[0]["execution_id"] == run_id
            action_id = pending[0]["action_id"]
            confirm = client.post(
                f"/api/permissions/pending/{action_id}/confirm",
                json={"approved": True, "once": True},
                headers=auth,
            )
            assert confirm.status_code == 200, confirm.text
            assert confirm.json()["once"] is True
            for _ in range(200):
                detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
                if detail["status"] != "running":
                    break
                time.sleep(0.05)
            assert detail["status"] == "success", detail
            assert _ProbeNode.calls == 1
            # 规则未持久化 — 同一工作流再跑一次仍暂停
            _ProbeNode.calls = 0
            run2 = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
            hitl2 = False
            for _ in range(200):
                if eng.hitl_pending(run2):
                    hitl2 = True
                    break
                time.sleep(0.05)
            assert hitl2, "once 批准不应持久化规则"
            # 收尾 run2, 避免悬挂任务
            action2 = client.get("/api/permissions/pending", headers=auth).json()["approvals"][0]["action_id"]
            client.post(f"/api/permissions/pending/{action2}/confirm", json={"approved": False}, headers=auth)
            for _ in range(200):
                d2 = client.get(f"/api/runs/{run2}", headers=auth).json()
                if d2["status"] != "running":
                    break
                time.sleep(0.05)
