"""#114 W9/B5: 交付审查状态机 (运行级, 区别于 W19 节点级 guard 审批)。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

STATIC_KEY = "fc-web-test-114"


def _seed(registry, run_id, status="success", review=None):
    registry._runs[run_id] = {
        "run_id": run_id,
        "workflow_id": "wf-1",
        "workflow_name": "wf",
        "status": status,
        "created_at": 1.0,
        "finished_at": time.time(),
        "steps": [],
        "result": None,
        "error": None,
        "review": review,
        "workflow_def": {"nodes": [], "edges": []},
    }


class TestRunReviewStateMachine:
    async def test_success_run_enters_pending_review(self):
        from fusion_cowork.server.web_api import RunRegistry

        class _Engine:
            async def execute(self, wf, initial_input=None, execution_id=""):
                return SimpleNamespace(
                    status=SimpleNamespace(value="success"),
                    steps=[],
                    result_summary="ok",
                    error=None,
                )

        registry = RunRegistry(_Engine())
        _seed(registry, "run_ok", status="running", review=None)
        await registry._execute("run_ok", SimpleNamespace(name="wf"))
        assert registry._runs["run_ok"]["status"] == "success"
        assert registry._runs["run_ok"]["review"]["status"] == "pending_review"

    async def test_failed_run_not_reviewable(self):
        from fusion_cowork.server.web_api import RunRegistry

        class _Engine:
            async def execute(self, wf, initial_input=None, execution_id=""):
                return SimpleNamespace(
                    status=SimpleNamespace(value="failed"),
                    steps=[],
                    result_summary="",
                    error="boom",
                )

        registry = RunRegistry(_Engine())
        _seed(registry, "run_bad", status="running", review=None)
        await registry._execute("run_bad", SimpleNamespace(name="wf"))
        assert registry._runs["run_bad"]["review"] is None
        assert registry.review("run_bad", True)[0] == "not_pending"

    def test_review_approve_and_reject(self):
        from fusion_cowork.server.web_api import RunRegistry

        registry = RunRegistry(engine=None)
        pending = {"status": "pending_review", "note": "", "reviewed_at": None}
        _seed(registry, "run_a", review=dict(pending))
        err, result = registry.review("run_a", True, "looks good")
        assert err is None
        assert result["review"]["status"] == "approved"
        assert result["review"]["note"] == "looks good"
        assert registry.review("run_a", True)[0] == "not_pending"

        _seed(registry, "run_b", review=dict(pending))
        err, result = registry.review("run_b", False, "redo node 2")
        assert err is None
        assert result["review"]["status"] == "rejected"
        assert result["workflow"] == {"nodes": [], "edges": []}

    def test_review_unknown_run(self):
        from fusion_cowork.server.web_api import RunRegistry

        assert RunRegistry(engine=None).review("missing", True)[0] == "not_found"


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


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestRunReviewRoutes:
    def test_approve_route(self, client):
        tc, app = client
        _seed(
            app.state.run_registry,
            "run_r1",
            review={"status": "pending_review", "note": "", "reviewed_at": None},
        )
        r = tc.post("/api/runs/run_r1/review", json={"approved": True}, headers=_auth())
        assert r.status_code == 200
        assert r.json()["review"]["status"] == "approved"

    def test_reject_returns_workflow(self, client):
        tc, app = client
        _seed(
            app.state.run_registry,
            "run_r2",
            review={"status": "pending_review", "note": "", "reviewed_at": None},
        )
        r = tc.post(
            "/api/runs/run_r2/review",
            json={"approved": False, "note": "needs iteration"},
            headers=_auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["review"]["status"] == "rejected"
        assert body["review"]["note"] == "needs iteration"
        assert body["workflow"] == {"nodes": [], "edges": []}

    def test_bad_body_and_state(self, client):
        tc, app = client
        _seed(app.state.run_registry, "run_r3", review=None)
        assert tc.post("/api/runs/run_r3/review", json={}, headers=_auth()).status_code == 400
        assert tc.post("/api/runs/run_r3/review", json={"approved": True}, headers=_auth()).status_code == 409
        assert tc.post("/api/runs/run_none/review", json={"approved": True}, headers=_auth()).status_code == 404

    def test_list_includes_review(self, client):
        tc, app = client
        _seed(
            app.state.run_registry,
            "run_r4",
            review={"status": "pending_review", "note": "", "reviewed_at": None},
        )
        body = tc.get("/api/runs", headers=_auth()).json()
        row = next(r for r in body["runs"] if r["run_id"] == "run_r4")
        assert row["review"]["status"] == "pending_review"
