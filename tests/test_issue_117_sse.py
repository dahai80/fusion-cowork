"""#117 B2: 运行步骤 SSE + 空间聊天 SSE (轮询为降级)。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

STATIC_KEY = "fc-web-test-117"


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app), app


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


def _route(app, path: str):
    for route in app.routes:
        if getattr(route, "path", "") == path:
            return route.endpoint
    raise AssertionError(f"route not found: {path}")


def _frames(response, count: int = 1):
    async def _collect():
        iterator = response.body_iterator
        chunks = []
        try:
            for _ in range(count):
                chunks.append(await iterator.__anext__())
        finally:
            await iterator.aclose()
        return chunks

    return asyncio.run(_collect())


def _seed_run(app, run_id: str = "run-117") -> None:
    app.state.run_registry._runs[run_id] = {
        "run_id": run_id,
        "workflow_id": "wf-117",
        "workflow_name": "演示流程",
        "status": "running",
        "created_at": time.time(),
        "finished_at": None,
        "error": None,
        "review": None,
        "steps": [],
        "artifacts": [],
        "directives": [],
        "workflow_def": {"nodes": [], "edges": []},
    }


class TestRunEventsSse:
    def test_unknown_run_404(self, client):
        tc, _ = client
        assert tc.get("/api/runs/missing/events", headers=_auth()).status_code == 404

    def test_snapshot_frame(self, client):
        _, app = client
        _seed_run(app)
        endpoint = _route(app, "/api/runs/{run_id}/events")

        async def _call():
            return await endpoint("run-117", _FakeRequest())

        response = asyncio.run(_call())
        assert response.media_type == "text/event-stream"
        frame = _frames(response)[0]
        event_line, data_line = frame.split("\n")[:2]
        assert event_line == "event: snapshot"
        assert data_line.startswith("data: ")
        payload = json.loads(data_line[len("data: ") :])
        assert payload["run"]["run_id"] == "run-117"
        assert "workflow_def" not in payload["run"]

    def test_broadcast_to_subscriber(self):
        from fusion_cowork.engine.events import EventEmitter
        from fusion_cowork.server.web_api import RunRegistry

        class _Engine:
            def get_execution(self, run_id):
                return None

        emitter = EventEmitter()
        registry = RunRegistry(_Engine(), event_emitter=emitter)
        registry._runs["run-117"] = {
            "run_id": "run-117",
            "status": "running",
            "steps": [],
            "workflow_def": {"nodes": []},
        }
        queue = registry.subscribe("run-117")

        async def _run():
            emitter.emit(
                emitter.create_event(
                    "node_end",
                    execution_id="run-117",
                    node_id="n1",
                    data={"ok": True},
                )
            )
            return await asyncio.wait_for(queue.get(), timeout=2)

        item = asyncio.run(_run())
        assert item["event"] == "node_end"
        assert item["node_id"] == "n1"
        assert item["run"]["status"] == "running"
        assert "workflow_def" not in item["run"]

        registry.unsubscribe("run-117", queue)
        assert not registry._subs.get("run-117")

    def test_unsubscribe_idempotent(self):
        from fusion_cowork.server.web_api import RunRegistry

        registry = RunRegistry(object())
        queue = registry.subscribe("x")
        registry.unsubscribe("x", queue)
        registry.unsubscribe("x", queue)
        assert registry._subs == {}


class TestSpaceSse:
    def test_ready_frame(self, client):
        tc, app = client
        created = tc.post("/api/space/spaces", json={"name": "SSE 空间"}, headers=_auth())
        assert created.status_code == 201, created.text
        space_id = created.json()["id"]
        endpoint = _route(app.state.space_api, "/spaces/{space_id}/stream")

        async def _call():
            return await endpoint(space_id, _FakeRequest())

        response = asyncio.run(_call())
        assert response.media_type == "text/event-stream"
        frame = _frames(response)[0]
        event_line, data_line = frame.split("\n")[:2]
        assert event_line == "event: ready"
        assert json.loads(data_line[len("data: ") :])["space_id"] == space_id
