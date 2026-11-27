"""issue #224: unified run view contract — runs_get full record (workflow_def topology + space_id ownership + directive echo) and SSE snapshot _public stripping."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from fusion_cowork.nodes import import_all_nodes

import_all_nodes()

STATIC_KEY = "test-key-224"

# Low-risk pure-logic nodes — no HITL gate / no LLM dependency, deterministic offline success
WF_DEF = {
    "name": "wf-224",
    "nodes": [
        {"id": "n1", "name": "merge", "config": {}},
        {"id": "n2", "name": "merge", "config": {}},
    ],
    "edges": [{"source_id": "n1", "target_id": "n2"}],
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app), app


def _headers():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


def _wait_terminal(tc, run_id, timeout=8.0):
    for _ in range(int(timeout * 20)):
        r = tc.get(f"/api/runs/{run_id}", headers=_headers())
        if r.status_code == 200 and r.json()["status"] not in ("running", "pending"):
            return r.json()
        time.sleep(0.05)
    raise AssertionError("run 未在超时内进入终态")  # i18n: user-facing


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


def _route(app, path):
    for route in app.routes:
        if getattr(route, "path", "") == path:
            return route.endpoint
    raise AssertionError(f"route {path} not found")


def _first_frame(response):
    async def _read():
        iterator = response.body_iterator
        try:
            return await iterator.__anext__()
        finally:
            await iterator.aclose()

    return asyncio.run(_read())


class TestRunViewContract:
    def test_runs_get_exposes_workflow_def_and_space_id(self, client):
        tc, app = client
        r = tc.post(
            "/api/space/spaces",
            json={"name": "目录224"},  # i18n: user-facing
            headers=_headers(),
        )
        sid = r.json()["id"]
        r = tc.post(
            "/api/runs",
            json={"workflow": WF_DEF, "name": "rv", "space_id": sid},
            headers=_headers(),
        )
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        rec = _wait_terminal(tc, run_id)
        # Topology source: full workflow_def present (frontend computeWaves layering depends on it)
        assert rec["workflow_def"]["nodes"][0]["id"] == "n1"
        assert rec["workflow_def"]["edges"][0] == {"source_id": "n1", "target_id": "n2"}
        # Ownership: workspace drawer depends on it
        assert rec["space_id"] == sid
        # Step shape: marker-step field present (regular step = False)
        assert all("user_correction" in s for s in rec["steps"])

    def test_directives_echoed_on_record(self, client):
        # Injection requires a running/paused run — seed a paused run (#227 pattern), verify directive echo in the runs_get full record
        tc, app = client
        registry = app.state.run_registry
        run_id = "run_224_dir"
        registry._runs[run_id] = {
            "run_id": run_id,
            "workflow_id": "wf-224",
            "workflow_name": "wf-224",
            "status": "paused",
            "created_at": 1.0,
            "finished_at": None,
            "steps": [],
            "result": None,
            "error": None,
        }
        registry._engine.add_directive = lambda rid, d: True
        r = tc.post(
            f"/api/runs/{run_id}/directive",
            json={"action": "inject", "instruction": "只看华东区"},  # i18n: user-facing
            headers=_headers(),
        )
        assert r.status_code == 202, r.text
        rec = tc.get(f"/api/runs/{run_id}", headers=_headers()).json()
        dirs = rec.get("directives") or []
        assert any(  # i18n: user-facing
            d.get("action") == "inject" and (d.get("payload") or {}).get("instruction") == "只看华东区" for d in dirs
        ), dirs

    def test_public_snapshot_strips_workflow_def_keeps_steps(self, client):
        from fusion_cowork.server.web_api import RunRegistry

        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rv3"}, headers=_headers())
        run_id = r.json()["run_id"]
        rec = _wait_terminal(tc, run_id)
        public = RunRegistry._public(rec)
        assert "workflow_def" not in public
        assert public["run_id"] == run_id
        assert isinstance(public["steps"], list) and len(public["steps"]) == 2
        # SSE snapshot is serializable (run-view realtime channel)
        json.dumps(public, ensure_ascii=False)

    def test_run_view_route_serves_snapshot(self, client):
        # Read the first frame via the endpoint directly — infinite SSE stream cannot be consumed via tc.stream
        # (test_issue_117 pattern)
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rv4"}, headers=_headers())
        run_id = r.json()["run_id"]
        _wait_terminal(tc, run_id)
        endpoint = _route(app, "/api/runs/{run_id}/events")
        response = asyncio.run(endpoint(run_id, _FakeRequest()))
        assert response.media_type == "text/event-stream"
        frame = _first_frame(response)
        event_line, data_line = frame.split("\n")[:2]
        assert event_line == "event: snapshot"
        payload = json.loads(data_line[len("data: ") :])
        assert payload["run"]["run_id"] == run_id
        assert "workflow_def" not in payload["run"]
