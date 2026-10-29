"""issue #207: run 归因 — source/labels 从 POST /api/runs 落到 run 记录, 随列表/详情/webhook 外发。

覆盖: HTTP 校验与透传、GET 列表/详情透出、run_finished webhook 载荷、
审批事件 run 归因 (run_source/run_labels, 不覆盖 approval 自身 source)。
"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.events import EventEmitter
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import WorkflowEngine
from fusion_cowork.server.web_api import RunRegistry, _make_pending_push_callback

STATIC_KEY = "test-key-207"
ECHO_NODE = "echo_probe_207"


@register_node
class _EchoNode(BaseNode):
    name = ECHO_NODE
    display_name = "Echo Probe 207"
    category = NodeCategory.LOGIC
    description = "测试用: 回显输入 (#207)"
    icon = "🧪"
    default_label = "Echo"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"echo": dict(inputs)}, summary="ok")


def _wf_def() -> dict:
    return {
        "name": "wf-207",
        "nodes": [{"id": "n1", "name": ECHO_NODE, "config": {}}],
        "edges": [],
    }


def _bad_wf_def() -> dict:
    return {
        "name": "wf-207-bad",
        "nodes": [{"id": "n1", "name": "nonexistent_type_207", "config": {}}],
        "edges": [],
    }


class _FakePush:
    def __init__(self):
        self.events = []

    async def push_event(self, event, payload):
        self.events.append((event, payload))


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setattr(web_api, "_module_key", lambda: "")
    monkeypatch.setattr(web_api, "_WEB_DIR", tmp_path / "web")
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


@pytest.fixture
def pm_no_guard(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_MEMORY_API_KEY", raising=False)
    return PermissionManager(level=PermissionLevel.CONFIRM)


def _stack(pm):
    emitter = EventEmitter()
    engine = WorkflowEngine(permission_manager=pm, event_emitter=emitter)
    registry = RunRegistry(engine, event_emitter=emitter)
    return engine, registry


async def _wait_terminal(registry, run_id, timeout: float = 5.0) -> str:
    for _ in range(int(timeout * 50)):
        rec = registry.get(run_id)
        status = str(rec.get("status", "")) if rec else ""
        if status not in ("running", "paused"):
            return status
        await asyncio.sleep(0.02)
    return "timeout"


class TestRunsCreateForwarding:
    def _capture_start(self, monkeypatch, client):
        started = []

        def _record_start(workflow_def, workflow_id="", workflow_name="", memory=True, message_input=None,
                          source="", labels=None):
            started.append(
                {
                    "workflow_def": workflow_def,
                    "memory": memory,
                    "message_input": message_input,
                    "source": source,
                    "labels": labels,
                }
            )
            return {"run_id": "run_stub", "status": "running"}

        monkeypatch.setattr(client.app.state.run_registry, "start", _record_start)
        return started

    def test_source_and_labels_forwarded(self, monkeypatch, client):
        started = self._capture_start(monkeypatch, client)
        r = client.post(
            "/api/runs",
            json={"name": "wf", "workflow": _wf_def(), "source": "email", "labels": {"tenant": "t1", "job": "j9"}},
            headers=_auth(),
        )
        assert r.status_code == 202, r.text
        assert started[0]["source"] == "email"
        assert started[0]["labels"] == {"tenant": "t1", "job": "j9"}

    def test_defaults_empty_source_and_labels(self, monkeypatch, client):
        started = self._capture_start(monkeypatch, client)
        r = client.post("/api/runs", json={"name": "wf", "workflow": _wf_def()}, headers=_auth())
        assert r.status_code == 202, r.text
        assert started[0]["source"] == ""
        assert started[0]["labels"] == {}

    def test_non_string_source_400(self, monkeypatch, client):
        self._capture_start(monkeypatch, client)
        r = client.post("/api/runs", json={"workflow": _wf_def(), "source": 7}, headers=_auth())
        assert r.status_code == 400
        assert "source" in r.json()["detail"]["message"]

    def test_labels_not_object_400(self, monkeypatch, client):
        self._capture_start(monkeypatch, client)
        r = client.post("/api/runs", json={"workflow": _wf_def(), "labels": ["a"]}, headers=_auth())
        assert r.status_code == 400
        assert "labels" in r.json()["detail"]["message"]

    def test_labels_non_string_values_400(self, monkeypatch, client):
        self._capture_start(monkeypatch, client)
        r = client.post("/api/runs", json={"workflow": _wf_def(), "labels": {"k": 1}}, headers=_auth())
        assert r.status_code == 400
        # JSON 对象键必为字符串, 非字符串值 (嵌套/list) 同样拒绝
        r2 = client.post("/api/runs", json={"workflow": _wf_def(), "labels": {"k": ["v"]}}, headers=_auth())
        assert r2.status_code == 400


class TestRunRecordSurfacing:
    def test_list_and_detail_surface_attribution(self, client):
        r = client.post(
            "/api/runs",
            json={"name": "wf", "workflow": _bad_wf_def(), "source": "email", "labels": {"tenant": "t1"}},
            headers=_auth(),
        )
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]

        listed = client.get("/api/runs", headers=_auth()).json()
        entry = next(x for x in listed["runs"] if x["run_id"] == run_id)
        assert entry["source"] == "email"
        assert entry["labels"] == {"tenant": "t1"}

        detail = client.get(f"/api/runs/{run_id}", headers=_auth()).json()
        assert detail["source"] == "email"
        assert detail["labels"] == {"tenant": "t1"}

    def test_unattributed_run_defaults(self, client):
        r = client.post("/api/runs", json={"workflow": _bad_wf_def()}, headers=_auth())
        run_id = r.json()["run_id"]
        detail = client.get(f"/api/runs/{run_id}", headers=_auth()).json()
        assert detail["source"] == ""
        assert detail["labels"] == {}


class TestWebhookAttribution:
    async def test_run_finished_payload_carries_source_and_labels(self, pm_no_guard):
        _engine, registry = _stack(pm_no_guard)
        fake = _FakePush()
        registry.push = fake
        started = registry.start(_wf_def(), workflow_name="wf-207", source="email", labels={"tenant": "t1"})
        run_id = started["run_id"]
        status = await _wait_terminal(registry, run_id)
        assert status == "success"
        for _ in range(100):
            if fake.events:
                break
            await asyncio.sleep(0.02)
        assert fake.events, "run_finished 未推送"
        event, payload = fake.events[0]
        assert event == "run_finished"
        assert payload["run_id"] == run_id
        assert payload["status"] == "success"
        assert payload["source"] == "email"
        assert payload["labels"] == {"tenant": "t1"}

    async def test_run_finished_defaults(self, pm_no_guard):
        _engine, registry = _stack(pm_no_guard)
        fake = _FakePush()
        registry.push = fake
        started = registry.start(_wf_def(), workflow_name="wf-207")
        status = await _wait_terminal(registry, started["run_id"])
        assert status == "success"
        for _ in range(100):
            if fake.events:
                break
            await asyncio.sleep(0.02)
        _event, payload = fake.events[0]
        assert payload["source"] == ""
        assert payload["labels"] == {}


class TestApprovalPushAttribution:
    async def test_pending_payload_enriched_with_run_attribution(self):
        _engine, registry = _stack(PermissionManager(level=PermissionLevel.CONFIRM))
        registry._runs["run_attr"] = {"source": "email", "labels": {"tenant": "t1"}}
        fake = _FakePush()
        cb = _make_pending_push_callback(fake, registry)
        cb({"action_id": "a1", "execution_id": "run_attr", "source": "guard", "tool_name": "shell_exec"})
        await asyncio.sleep(0)
        assert fake.events
        event, payload = fake.events[0]
        assert event == "approval_requested"
        # approval 自身 source (guard/local) 保留, run 归因走独立键
        assert payload["source"] == "guard"
        assert payload["run_source"] == "email"
        assert payload["run_labels"] == {"tenant": "t1"}

    async def test_unknown_execution_still_pushes_without_attribution(self):
        _engine, registry = _stack(PermissionManager(level=PermissionLevel.CONFIRM))
        fake = _FakePush()
        cb = _make_pending_push_callback(fake, registry)
        cb({"action_id": "a2", "execution_id": "missing_run", "source": "local"})
        await asyncio.sleep(0)
        _event, payload = fake.events[0]
        assert "run_source" not in payload
        assert "run_labels" not in payload

    async def test_without_registry_no_attribution(self):
        fake = _FakePush()
        cb = _make_pending_push_callback(fake)
        cb({"action_id": "a3", "execution_id": "run_attr", "source": "guard"})
        await asyncio.sleep(0)
        _event, payload = fake.events[0]
        assert payload["source"] == "guard"
        assert "run_source" not in payload

    def test_no_running_loop_skips_silently(self, caplog):
        fake = _FakePush()
        cb = _make_pending_push_callback(fake)
        cb({"action_id": "a4", "execution_id": "x"})
        assert fake.events == []
