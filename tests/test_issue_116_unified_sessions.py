"""#116 W11/A12: 统一会话列表 — engine / space / scheduler 合并 + source 过滤。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

STATIC_KEY = "fc-web-test-116"


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    class _Scheduler:
        def list_tasks(self):
            return [
                SimpleNamespace(
                    id="task1",
                    name="每夜报表",
                    workflow_id="wf-1",
                    trigger_type="cron",
                    status=SimpleNamespace(value="active"),
                    created_at=100.0,
                    last_run=time.time(),
                    run_count=3,
                    fail_count=1,
                    tenant_id="default",
                )
            ]

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
        scheduler=_Scheduler(),
    )
    return TestClient(app), app


class TestUnifiedSessions:
    def test_space_and_scheduler_merged(self, client):
        tc, _ = client
        created = tc.post("/api/space/spaces", json={"name": "研究空间"}, headers=_auth())
        assert created.status_code == 201, created.text
        space_id = created.json()["id"]
        sent = tc.post(
            f"/api/space/spaces/{space_id}/messages",
            json={"user_id": "local_user", "content": "hello space"},
            headers=_auth(),
        )
        assert sent.status_code in (200, 201), sent.text

        body = tc.get("/api/sessions", headers=_auth()).json()
        sources = {s["source"] for s in body["sessions"]}
        assert {"space", "scheduler"} <= sources

        space_item = next(s for s in body["sessions"] if s["source"] == "space")
        assert space_item["space_id"] == space_id
        assert space_item["title"] == "研究空间"
        assert space_item["metadata"]["last_message"] == "hello space"
        assert space_item["href"] == "#/spaces"

        sched_item = next(s for s in body["sessions"] if s["source"] == "scheduler")
        assert sched_item["id"] == "sched_task1"
        assert sched_item["metadata"]["run_count"] == 3
        assert sched_item["href"] == "#/schedule"

    def test_source_filter(self, client):
        tc, _ = client
        body = tc.get("/api/sessions?source=scheduler", headers=_auth()).json()
        assert body["count"] == 1
        assert body["sessions"][0]["source"] == "scheduler"

        body = tc.get("/api/sessions?source=engine", headers=_auth()).json()
        assert all(s["source"] == "engine" for s in body["sessions"])

    def test_invalid_source(self, client):
        tc, _ = client
        assert tc.get("/api/sessions?source=bogus", headers=_auth()).status_code == 400

    def test_sorted_by_updated_at_desc(self, client):
        tc, _ = client
        body = tc.get("/api/sessions", headers=_auth()).json()
        stamps = [s["updated_at"] for s in body["sessions"]]
        assert stamps == sorted(stamps, reverse=True)
