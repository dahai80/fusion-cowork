"""issue #225: 内联审批 — 审批卡 "编辑参数" 覆盖并入被闸节点输入 + confirm 路由 edited_params 透传。"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus

HITL_NODE = "hitl_probe_225"


@register_node
class _ProbeNode(BaseNode):
    name = HITL_NODE
    display_name = "HITL Probe 225"
    category = NodeCategory.TOOL
    description = "测试用: 高风险探针 (#225)"
    icon = "🧪"
    default_label = "Probe"

    calls = 0
    received: dict = {}

    async def execute(self, inputs: dict) -> NodeResult:
        type(self).calls += 1
        type(self).received = dict(inputs)
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")


@pytest.fixture
def pm_no_guard(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_TIMEOUT", raising=False)
    from fusion_cowork.engine import permission_manager

    monkeypatch.setattr(permission_manager, "HIGH_RISK_NODES", permission_manager.HIGH_RISK_NODES | {HITL_NODE})
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _wf():
    from fusion_cowork.engine.node import NodeConfig

    wf = Workflow(name="hitl-wf-225")
    probe = NodeRegistry.create(HITL_NODE, config=NodeConfig(params={"amount": 100}))
    wf.add_node(probe)
    return wf


async def _wait_hitl(engine: WorkflowEngine, exec_id: str, timeout: float = 5.0):
    for _ in range(int(timeout * 50)):
        if engine.hitl_pending(exec_id):
            return True
        await asyncio.sleep(0.02)
    return False


class TestHitlParamOverride:
    async def test_edited_params_merge_into_gated_node_input(self, pm_no_guard):
        # 编辑参数 + 一次性批准 → 覆盖并入本节点输入, 原参数保留未覆盖键
        _ProbeNode.calls = 0
        _ProbeNode.received = {}
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        exec_id = "run-225-edit"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        assert eng.resolve_hitl(exec_id, True, once=True, params={"amount": 10, "payee": "acme"})
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.SUCCESS
        assert _ProbeNode.calls == 1
        assert _ProbeNode.received["amount"] == 10
        assert _ProbeNode.received["payee"] == "acme"
        # 覆盖一次性消费 — 不残留
        assert eng._hitl_param_overrides == {}

    async def test_params_ignored_on_deny(self, pm_no_guard):
        # 拒绝时 params 不生效, 节点不执行, 覆盖不残留
        _ProbeNode.calls = 0
        eng = WorkflowEngine(permission_manager=pm_no_guard)
        exec_id = "run-225-deny"
        task = asyncio.create_task(eng.execute(_wf(), execution_id=exec_id))
        assert await _wait_hitl(eng, exec_id)
        assert eng.resolve_hitl(exec_id, False, params={"amount": 1})
        execution = await asyncio.wait_for(task, timeout=5)
        assert execution.status == WorkflowStatus.FAILED
        assert _ProbeNode.calls == 0
        assert eng._hitl_param_overrides == {}


STATIC_KEY = "test-key-225"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"

    calls: list[dict] = []

    class _EngineStub:
        # issue #303: engine contract grew attribution (and #309 batch) kwargs
        def resolve_hitl(self, execution_id, approved, once=False, params=None, attribution=None, batch=None):
            calls.append({"exec": execution_id, "approved": approved, "once": once, "params": params})
            return True

    pm = PermissionManager(level=PermissionLevel.CONFIRM)
    app = web_api.create_web_app(
        engine=_EngineStub(),
        permission_manager=pm,
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    registry = app.state.run_registry
    registry._runs["run_225"] = {
        "run_id": "run_225",
        "workflow_id": "wf-225",
        "workflow_name": "wf-225",
        "status": "paused",
        "created_at": 1.0,
        "finished_at": None,
        "steps": [],
        "result": None,
        "error": None,
    }
    # 本地待审批: 绑定 run_225, 带原参数
    pm._pending_local_approvals["act_225"] = HITL_NODE
    pm._pending_local_details["act_225"] = {
        "tool_name": HITL_NODE,
        "params": {"amount": 100},
        "execution_id": "run_225",
        "node_id": "n1",
    }
    return TestClient(app), calls


class TestConfirmRouteEditedParams:
    def test_edited_params_passed_to_resolver(self, client):
        tc, calls = client
        r = tc.post(
            "/api/permissions/pending/act_225/confirm",
            json={"approved": True, "once": True, "edited_params": {"amount": 10}},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["once"] is True
        assert calls == [{"exec": "run_225", "approved": True, "once": True, "params": {"amount": 10}}]

    def test_non_dict_edited_params_ignored(self, client):
        tc, calls = client
        r = tc.post(
            "/api/permissions/pending/act_225/confirm",
            json={"approved": True, "once": True, "edited_params": "amount=10"},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert r.status_code == 200, r.text
        assert calls and calls[0]["params"] is None

    def test_edited_params_rejected_also_wakes_run(self, client):
        # 拒绝路径 (#216) 即唤醒, 不带参数覆盖 — resolver 收 approved=False params=None
        tc, calls = client
        r = tc.post(
            "/api/permissions/pending/act_225/confirm",
            json={"approved": False, "once": True},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert r.status_code == 200, r.text
        assert calls == [{"exec": "run_225", "approved": False, "once": False, "params": None}]
