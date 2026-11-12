"""issue #459: inbound triggers — webhook/事件新建 run + wait_for_event 节点。

事件驱动链路起点: token 鉴权 hook 建新 run; wait_for_event 在事件总线命名事件
到达后继续; 触发器 hook 同时广播 trigger.<name> 事件可链式联动。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from fusion_cowork.engine.event_bus import EventBus, get_event_bus
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.triggers import TriggerRegistry


@register_node
class _Noop459(BaseNode):
    name = "noop459"
    display_name = "Noop 459"
    category = NodeCategory.LOGIC
    description = "test probe: trigger target node (#459)"
    icon = "🧪"
    default_label = "Noop"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"done": True}, summary="noop")


class TestTriggerRegistry:
    def test_create_and_persist_roundtrip(self, tmp_path):
        root = tmp_path / "store"
        reg = TriggerRegistry(root)
        rec = reg.create("nightly", "wf_abc", params={"env": "prod"})
        assert rec["id"].startswith("trg_")
        assert rec["token"].startswith("trgk_")
        reg2 = TriggerRegistry(root)
        found = reg2.find_by_token(rec["token"])
        assert found and found["name"] == "nightly" and found["workflow_id"] == "wf_abc"

    def test_create_validation(self, tmp_path):
        reg = TriggerRegistry(tmp_path)
        with pytest.raises(ValueError, match="name"):
            reg.create("", "wf_x")
        with pytest.raises(ValueError, match="workflow_id"):
            reg.create("t", " ")

    def test_list_masks_token(self, tmp_path):
        reg = TriggerRegistry(tmp_path)
        rec = reg.create("t1", "wf_x")
        items = reg.list()
        assert items[0]["token"].endswith("…")
        assert rec["token"] not in str(items)

    def test_delete(self, tmp_path):
        reg = TriggerRegistry(tmp_path)
        rec = reg.create("t", "wf_x")
        assert reg.delete(rec["id"]) is True
        assert reg.delete(rec["id"]) is False
        assert reg.find_by_token(rec["token"]) is None

    def test_find_by_token_fail_closed(self, tmp_path):
        reg = TriggerRegistry(tmp_path)
        reg.create("t", "wf_x")
        assert reg.find_by_token("") is None
        assert reg.find_by_token("trgk_wrong") is None


class TestEventBus:
    async def test_wait_wakes_on_emit(self):
        bus = EventBus()

        async def later():
            await asyncio.sleep(0.05)
            bus.emit("go", {"k": "v"})

        asyncio.create_task(later())
        result = await bus.wait("go", timeout=2)
        assert result == {"event": "go", "payload": {"k": "v"}}

    async def test_wait_timeout_raises(self):
        bus = EventBus()
        with pytest.raises(TimeoutError):
            await bus.wait("never", timeout=0.05)

    async def test_emit_no_waiters_is_noop(self):
        bus = EventBus()
        assert bus.emit("nobody") == 0

    async def test_waiter_cleaned_after_timeout(self):
        bus = EventBus()
        with pytest.raises(TimeoutError):
            await bus.wait("x", timeout=0.05)
        assert "x" not in bus.pending_names()

    def test_singleton(self):
        assert get_event_bus() is get_event_bus()


class TestWaitForEventNode:
    def _node(self, **params):
        from fusion_cowork.nodes.logic.logic_nodes import WaitForEventNode

        node = WaitForEventNode()
        node.config.params = params
        return node

    async def test_success_on_event(self):
        bus = get_event_bus()

        async def later():
            await asyncio.sleep(0.02)
            bus.emit("deploy.done", {"sha": "abc"})

        asyncio.create_task(later())
        res = await self._node(event="deploy.done", timeout=2).execute({})
        assert res.status.value == "success"
        assert res.data == {"event": "deploy.done", "payload": {"sha": "abc"}}

    async def test_timeout_structured_failure(self):
        res = await self._node(event="ghost", timeout=0.05).execute({})
        assert res.status.value == "failed"
        assert "超时" in res.error and "ghost" in res.error

    async def test_missing_event_param(self):
        res = await self._node().execute({})
        assert res.status.value == "failed"
        assert "event" in res.error

    async def test_registered_with_alias(self):
        from fusion_cowork import NODE_NAME_ALIASES
        from fusion_cowork.engine.node import NodeRegistry
        from fusion_cowork.nodes import import_all_nodes

        import_all_nodes()
        assert NodeRegistry.get("wait_for_event") is not None
        assert NODE_NAME_ALIASES.get("等待事件") == "wait_for_event"
        assert NODE_NAME_ALIASES.get("事件等待") == "wait_for_event"
        NodeRegistry.register_alias("等待事件", "wait_for_event")
        assert NodeRegistry.resolve_alias("等待事件") == "wait_for_event"


class TestTriggerAPI:
    # NOTE: TestClient must be entered (`with`) — a bare TestClient spins a
    # fresh portal (event loop) per request, and the run task created inside
    # POST /api/runs gets cancelled when that request's loop tears down.
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k459")
        eng = _quiet_engine()
        app = web_api.create_web_app(engine=eng, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        with TestClient(app) as client:
            yield client, {"Authorization": "Bearer k459"}, eng

    def test_trigger_lifecycle_and_run_creation(self, api):
        client, auth, eng = api
        wf_def = {
            "name": "trig-wf",
            "workflow": {"nodes": [{"id": "n1", "name": "noop459", "config": {}}], "edges": []},
        }
        wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
        created = client.post("/api/triggers", json={"name": "nightly", "workflow_id": wf_id}, headers=auth)
        assert created.status_code == 201, created.text
        token = created.json()["token"]
        # list 响应不含明文 token
        listed = client.get("/api/triggers", headers=auth).json()
        assert listed["count"] == 1 and token not in str(listed)
        # token 鉴权 hook 建新 run
        hook = client.post(f"/api/hooks/trigger/{token}", json={"env": "prod"})
        assert hook.status_code == 202, hook.text
        run_id = hook.json()["run_id"]
        assert hook.json()["started"] is True
        detail = {}
        for _ in range(200):
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            if detail.get("status") != "running":
                break
            time.sleep(0.05)
        assert detail["status"] == "success", detail
        rec = client.get(f"/api/runs/{run_id}", headers=auth).json()
        assert rec.get("source") == "trigger" or rec.get("labels", {}).get("trigger") == "nightly", rec
        # 删除触发器后 token 失效
        tid = created.json()["id"]
        assert client.delete(f"/api/triggers/{tid}", headers=auth).status_code == 200
        assert client.post(f"/api/hooks/trigger/{token}", json={}).status_code == 401

    def test_hook_bad_token_and_bad_json(self, api):
        client, auth, _ = api
        assert client.post("/api/hooks/trigger/trgk_wrong", json={}).status_code == 401
        wf_id = client.post(
            "/api/workflows",
            json={"name": "w", "workflow": {"nodes": [{"id": "n1", "name": "noop459", "config": {}}], "edges": []}},
            headers=auth,
        ).json()["id"]
        token = client.post("/api/triggers", json={"name": "t", "workflow_id": wf_id}, headers=auth).json()["token"]
        r = client.post(
            f"/api/hooks/trigger/{token}",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_trigger_requires_existing_workflow(self, api):
        client, auth, _ = api
        r = client.post("/api/triggers", json={"name": "t", "workflow_id": "wf_ghost"}, headers=auth)
        assert r.status_code == 400

    def test_trigger_emit_feeds_wait_for_event(self, api):
        client, auth, eng = api
        wf_def = {
            "name": "chain-wf",
            "workflow": {
                "nodes": [
                    {
                        "id": "n1",
                        "name": "wait_for_event",
                        "config": {"params": {"event": "trigger.nightly", "timeout": 10}},
                    }
                ],
                "edges": [],
            },
        }
        wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
        run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
        # 等 run 进入事件等待 (总线上出现 waiter), 再用触发器 hook 唤醒
        token = client.post("/api/triggers", json={"name": "nightly", "workflow_id": wf_id}, headers=auth).json()[
            "token"
        ]
        _wait_bus_waiter("trigger.nightly")
        hook = client.post(f"/api/hooks/trigger/{token}", json={"ping": 1})
        assert hook.status_code == 202
        for _ in range(200):
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            if detail.get("status") != "running":
                break
            time.sleep(0.05)
        steps = [s for s in detail.get("steps", []) if s.get("node_id") == "n1" and s.get("status") != "blocked"]
        assert detail["status"] == "success", detail
        assert steps, steps
        payload = steps[-1]["output_data"]["payload"]
        assert payload.get("ping") == 1 and payload.get("run_id", "").startswith("run_"), payload

    def test_events_emit_endpoint(self, api):
        client, auth, _ = api
        wf_def = {
            "name": "ev-wf",
            "workflow": {
                "nodes": [
                    {"id": "n1", "name": "wait_for_event", "config": {"params": {"event": "manual.go", "timeout": 10}}}
                ],
                "edges": [],
            },
        }
        wf_id = client.post("/api/workflows", json=wf_def, headers=auth).json()["id"]
        run_id = client.post("/api/runs", json={"workflow_id": wf_id}, headers=auth).json()["run_id"]
        _wait_bus_waiter("manual.go")
        r = client.post("/api/events/manual.go", json={"ok": True}, headers=auth)
        assert r.status_code == 200 and r.json()["woken"] >= 1
        for _ in range(200):
            detail = client.get(f"/api/runs/{run_id}", headers=auth).json()
            if detail.get("status") != "running":
                break
            time.sleep(0.05)
        assert detail["status"] == "success", detail


def _quiet_engine():
    from fusion_cowork.engine import PermissionManager, WorkflowEngine

    return WorkflowEngine(permission_manager=PermissionManager())


def _wait_bus_waiter(event_name: str, timeout_s: float = 5.0):
    from fusion_cowork.engine.event_bus import get_event_bus

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if event_name in get_event_bus().pending_names():
            return
        time.sleep(0.05)
    raise AssertionError(f"event waiter not registered within {timeout_s}s: {event_name}")
