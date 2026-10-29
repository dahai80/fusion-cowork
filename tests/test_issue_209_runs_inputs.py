"""issue #209: POST /api/runs 的 per-run 输入 — inputs (initial_input 别名) 注入首节点。

覆盖: 路由透传 message_input、非对象 400、别名与优先级、真实引擎首节点输入快照。
"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.events import EventEmitter
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.workflow import WorkflowEngine
from fusion_cowork.server.web_api import RunRegistry

STATIC_KEY = "test-key-209"
ECHO_NODE = "echo_probe_209"


@register_node
class _EchoNode(BaseNode):
    name = ECHO_NODE
    display_name = "Echo Probe 209"
    category = NodeCategory.LOGIC
    description = "测试用: 回显输入 (#209)"
    icon = "🧪"
    default_label = "Echo"

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"echo": dict(inputs)}, summary="ok")


def _wf_def() -> dict:
    return {
        "name": "wf-209",
        "nodes": [{"id": "n1", "name": ECHO_NODE, "config": {}}],
        "edges": [],
    }


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


async def _wait_terminal(registry, run_id, timeout: float = 5.0) -> str:
    for _ in range(int(timeout * 50)):
        rec = registry.get(run_id)
        status = str(rec.get("status", "")) if rec else ""
        if status not in ("running", "paused"):
            return status
        await asyncio.sleep(0.02)
    return "timeout"


class TestRunsCreateInputs:
    def _capture_start(self, monkeypatch, client):
        started = []

        def _record_start(workflow_def, workflow_id="", workflow_name="", memory=True, message_input=None,
                          source="", labels=None):
            started.append({"message_input": message_input})
            return {"run_id": "run_stub", "status": "running"}

        monkeypatch.setattr(client.app.state.run_registry, "start", _record_start)
        return started

    def test_inputs_forwarded_as_message_input(self, monkeypatch, client):
        started = self._capture_start(monkeypatch, client)
        r = client.post("/api/runs", json={"workflow": _wf_def(), "inputs": {"ticket": "T-1"}}, headers=_auth())
        assert r.status_code == 202, r.text
        assert started[0]["message_input"] == {"ticket": "T-1"}

    def test_initial_input_alias(self, monkeypatch, client):
        started = self._capture_start(monkeypatch, client)
        r = client.post("/api/runs", json={"workflow": _wf_def(), "initial_input": {"ticket": "T-2"}}, headers=_auth())
        assert r.status_code == 202, r.text
        assert started[0]["message_input"] == {"ticket": "T-2"}

    def test_inputs_win_over_alias(self, monkeypatch, client):
        started = self._capture_start(monkeypatch, client)
        r = client.post(
            "/api/runs",
            json={"workflow": _wf_def(), "inputs": {"a": "1"}, "initial_input": {"b": "2"}},
            headers=_auth(),
        )
        assert r.status_code == 202, r.text
        assert started[0]["message_input"] == {"a": "1"}

    def test_omitted_inputs_none(self, monkeypatch, client):
        started = self._capture_start(monkeypatch, client)
        client.post("/api/runs", json={"workflow": _wf_def()}, headers=_auth())
        assert started[0]["message_input"] is None

    @pytest.mark.parametrize("bad", [["x"], "text", 3, True])
    def test_non_object_inputs_400(self, monkeypatch, client, bad):
        started = self._capture_start(monkeypatch, client)
        r = client.post("/api/runs", json={"workflow": _wf_def(), "inputs": bad}, headers=_auth())
        assert r.status_code == 400
        assert "inputs" in r.json()["detail"]["message"]
        assert started == []

    def test_non_object_initial_input_400(self, monkeypatch, client):
        started = self._capture_start(monkeypatch, client)
        r = client.post("/api/runs", json={"workflow": _wf_def(), "initial_input": "nope"}, headers=_auth())
        assert r.status_code == 400
        assert started == []


class TestInputInjectionEndToEnd:
    async def test_message_input_reaches_first_node(self, pm_no_guard):
        emitter = EventEmitter()
        engine = WorkflowEngine(permission_manager=pm_no_guard, event_emitter=emitter)
        registry = RunRegistry(engine, event_emitter=emitter)
        payload = {"ticket": "T-1", "notes": ["a", "b"]}
        started = registry.start(_wf_def(), workflow_name="wf-209", message_input=payload)
        run_id = started["run_id"]
        status = await _wait_terminal(registry, run_id)
        assert status == "success"
        record = registry.get(run_id)
        assert record["steps"][0]["input_data"] == payload
        assert record["steps"][0]["node_name"] == ECHO_NODE

    async def test_no_message_input_first_node_empty(self, pm_no_guard):
        emitter = EventEmitter()
        engine = WorkflowEngine(permission_manager=pm_no_guard, event_emitter=emitter)
        registry = RunRegistry(engine, event_emitter=emitter)
        started = registry.start(_wf_def(), workflow_name="wf-209")
        status = await _wait_terminal(registry, started["run_id"])
        assert status == "success"
        record = registry.get(started["run_id"])
        assert record["steps"][0]["input_data"] == {}
