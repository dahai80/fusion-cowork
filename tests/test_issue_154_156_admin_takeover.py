"""issue #154 + #156: 跨设备接管 / 推送出口 / 管理台 / OTel 审计导出。

Covered:
- PushEgress: 未配置 no-op; 配置后 webhook 送达 (MockTransport), 失败不抛
- 审批入队回调 on_pending_created (本地高风险拒绝)
- RunRegistry 终态 push (run_finished)
- WS /ws/takeover/{run_id}: 快照帧, 转向帧, 认证失败 4401, 运行不存在 4404
- 管理台: admin key 503/401, members/usage, audit export OTLP 形状, push test
"""

from __future__ import annotations

import asyncio
import json

import pytest

STATIC_KEY = "fc-web-test-154"
ADMIN_KEY = "fc-admin-test-156"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fusion_cowork.engine import WorkflowEngine
    from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node
    from fusion_cowork.server import web_api

    saved = dict(NodeRegistry._registry)
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setenv("FUSION_COWORK_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.delenv("FUSION_COWORK_PUSH_WEBHOOK_URL", raising=False)

    @register_node
    class MockSleepNode154(BaseNode):
        name = "mock_sleep_154"
        display_name = "慢回显"
        category = NodeCategory.TOOL

        async def execute(self, params):
            await asyncio.sleep(0.3)
            return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="sleep ok")

    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        engine=WorkflowEngine(),
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
    )
    yield TestClient(app)
    NodeRegistry._registry.clear()
    NodeRegistry._registry.update(saved)


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}", "x-api-key": STATIC_KEY}


def _admin(extra=None):
    headers = {**_auth(), "x-admin-key": ADMIN_KEY}
    if extra:
        headers.update(extra)
    return headers


@pytest.fixture
def run_id(client):
    resp = client.post(
        "/api/runs",
        headers=_auth(),
        json={"workflow": {"name": "sleep-wf", "nodes": [{"id": "n1", "name": "mock_sleep_154"}], "edges": []}},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["run_id"]


# ── PushEgress ─────────────────────────────────────────────────────────────


class TestPushEgress:
    def test_unset_is_noop(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_PUSH_WEBHOOK_URL", raising=False)
        from fusion_cowork.server.push import PushEgress

        egress = PushEgress.from_env()
        assert egress.enabled is False

    async def test_webhook_delivery_and_headers(self):
        import httpx

        from fusion_cowork.server.push import PushEgress

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["secret"] = request.headers.get("x-fusion-push-secret")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        egress = PushEgress(url="http://hook.local/x", secret="s3cret", transport=httpx.MockTransport(handler))
        delivered = await egress.push_event("run_finished", {"run_id": "r1"})
        assert delivered is True
        assert captured["url"] == "http://hook.local/x"
        assert captured["secret"] == "s3cret"
        assert captured["body"]["kind"] == "run_finished"
        assert captured["body"]["payload"]["run_id"] == "r1"

    async def test_webhook_failure_is_swallowed(self):
        import httpx

        from fusion_cowork.server.push import PushEgress

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        egress = PushEgress(url="http://hook.local/x", transport=httpx.MockTransport(handler))
        assert await egress.push_event("k", {}) is False

        def raise_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        egress2 = PushEgress(url="http://hook.local/x", transport=httpx.MockTransport(raise_handler))
        assert await egress2.push_event("k", {}) is False


# ── 审批回调 / run push ─────────────────────────────────────────────────────


class TestPendingPushHooks:
    async def test_on_pending_created_fires_for_local_high_risk(self):
        from fusion_cowork.engine import PermissionLevel
        from fusion_cowork.engine.permission import PermissionManager

        mgr = PermissionManager(level=PermissionLevel.CONFIRM)
        seen = []
        mgr.on_pending_created = seen.append
        allowed = await mgr.check("clipboard", action="read")
        assert allowed is False, "高风险节点无批准必须拒绝"
        assert len(seen) == 1
        info = seen[0]
        assert info["tool_name"] == "clipboard"
        assert info["source"] == "local"
        assert info["action_id"].startswith("local_")

    async def test_run_finished_pushes(self):
        from fusion_cowork.engine import WorkflowEngine
        from fusion_cowork.engine.node import (
            BaseNode,
            NodeCategory,
            NodeRegistry,
            NodeResult,
            NodeStatus,
            register_node,
        )
        from fusion_cowork.server.web_api import RunRegistry

        class _FakePush:
            def __init__(self):
                self.events = []

            async def push_event(self, kind, payload):
                self.events.append((kind, payload))
                return True

        saved = dict(NodeRegistry._registry)

        @register_node
        class PushMockNode(BaseNode):
            name = "push_mock_154"
            display_name = "推送回显"
            category = NodeCategory.TOOL

            async def execute(self, params):
                return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="ok")

        try:
            registry = RunRegistry(WorkflowEngine())
            fake = _FakePush()
            registry.push = fake
            wf = {
                "name": "push-wf",
                "nodes": [{"id": "n1", "name": "push_mock_154"}],
                "edges": [],
            }
            started = registry.start(wf)
            rid = started["run_id"]
            for _ in range(50):
                if registry.get(rid)["status"] not in ("running", "paused"):
                    break
                await asyncio.sleep(0.1)
            for _ in range(20):
                if fake.events:
                    break
                await asyncio.sleep(0.05)
            assert fake.events, "run 终态应触发 webhook push"
            kind, payload = fake.events[0]
            assert kind == "run_finished"
            assert payload["run_id"] == rid
        finally:
            NodeRegistry._registry.clear()
            NodeRegistry._registry.update(saved)


# ── WS takeover ────────────────────────────────────────────────────────────


class TestTakeoverWs:
    def test_snapshot_and_frames(self, client, run_id):
        with client.websocket_connect(f"/ws/takeover/{run_id}?token={STATIC_KEY}") as ws:
            snap = ws.receive_json()
            assert snap["event"] == "snapshot"
            assert snap["run"]["run_id"] == run_id
            ws.send_text("not json")
            err = ws.receive_json()
            assert err["event"] == "error"
            ws.send_text(json.dumps({"method": "nope", "params": {}}))
            err2 = ws.receive_json()
            assert err2["event"] == "error"
            assert "unknown method" in err2["message"]
            ws.send_text(json.dumps({"method": "directive", "params": {"action": "bad_action"}}))
            err3 = ws.receive_json()
            assert err3["event"] == "error"
            assert "bad_action" in err3["message"]

    def test_bad_token_closes_4401(self, client, run_id):
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/ws/takeover/{run_id}?token=wrong") as ws,
        ):
            ws.receive_json()

    def test_unknown_run_4404(self, client):
        with client.websocket_connect(f"/ws/takeover/run_missing?token={STATIC_KEY}") as ws:
            frame = ws.receive_json()
            assert frame["event"] == "error"
            assert "不存在" in frame["message"]


# ── 管理台 ─────────────────────────────────────────────────────────────────


class TestAdminConsole:
    def test_admin_key_503_unset(self, client, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_ADMIN_KEY", raising=False)
        resp = client.get("/api/admin/members", headers=_auth())
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "admin_disabled"

    def test_admin_key_401_wrong(self, client):
        resp = client.get("/api/admin/members", headers={**_auth(), "x-admin-key": "wrong"})
        assert resp.status_code == 401

    def test_members_and_usage(self, client):
        resp = client.get("/api/admin/members", headers=_admin())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] in ("identity", "local")
        assert isinstance(body["members"], list)

        usage = client.get("/api/admin/usage", headers=_admin())
        assert usage.status_code == 200, usage.text
        ubody = usage.json()
        for key in ("spaces", "messages", "artifacts", "agents"):
            assert key in ubody["spaces"]
        assert "by_status" in ubody["runs"]
        assert isinstance(ubody["tokens"], int)

    def test_audit_export_otlp(self, client):
        registry = client.app.state.run_registry
        # 直接构造 fixture run 记录 (含终态时间 + 失败错误)
        registry._runs["run_audit_1"] = {
            "run_id": "run_audit_1",
            "workflow_id": "wf_a",
            "workflow_name": "审计工作流",
            "status": "failed",
            "created_at": 1.0,
            "finished_at": 2.0,
            "error": "boom",
            "steps": [{"node_id": "n1"}],
        }
        resp = client.get("/api/admin/audit/export", headers=_admin())
        assert resp.status_code == 200, resp.text
        data = resp.json()
        logs = data["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        assert logs, "导出应至少含 run.finished 记录"
        attrs = {a["key"]: a["value"]["stringValue"] for a in logs[0]["attributes"]}
        assert attrs["event.name"] == "run.finished"
        assert attrs["run_id"] == "run_audit_1"
        assert logs[0]["severityText"] == "ERROR"

    def test_push_test_disabled(self, client):
        resp = client.post("/api/admin/push/test", headers=_admin())
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["delivered"] is False
