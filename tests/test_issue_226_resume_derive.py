"""issue #226: 终态 run 从指定步骤重跑 — 新 run 链回原 run (labels.resume_of), 已完成步骤快照续跑, 校验/终态守卫。"""

from __future__ import annotations

import pytest

from fusion_cowork.nodes import import_all_nodes

import_all_nodes()

STATIC_KEY = "test-key-226"

# 低风险纯逻辑节点 — 离线确定性成功, 无 HITL/LLM 依赖
WF_DEF = {
    "name": "wf-226",
    "nodes": [
        {"id": "n1", "name": "merge", "config": {}},
        {"id": "n2", "name": "merge", "config": {}},
        {"id": "n3", "name": "merge", "config": {}},
    ],
    "edges": [
        {"source_id": "n1", "target_id": "n2"},
        {"source_id": "n2", "target_id": "n3"},
    ],
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
    import time

    for _ in range(int(timeout * 20)):
        r = tc.get(f"/api/runs/{run_id}", headers=_headers())
        if r.status_code == 200 and r.json()["status"] not in ("running", "pending"):
            return r.json()
        time.sleep(0.05)
    raise AssertionError("run 未在超时内进入终态")


class TestResumeFrom:
    def test_resume_from_step_creates_linked_run_with_snapshot(self, client):
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rv226"}, headers=_headers())
        assert r.status_code == 202, r.text
        orig_id = r.json()["run_id"]
        orig = _wait_terminal(tc, orig_id)
        assert orig["status"] == "success" and len(orig["steps"]) == 3

        # 从 n2 重跑: n1 的成功快照续跑, n2/n3 真正重执行
        r = tc.post(f"/api/runs/{orig_id}/resume-from", json={"node_id": "n2"}, headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        new_id = body["run_id"]
        assert body["resume_of"] == orig_id
        assert body["resumed_from"] == "n2"
        assert body["snapshot_steps"] == 1

        rec = _wait_terminal(tc, new_id)
        # 新 run 链回原 run, workspace 归属继承
        assert rec["labels"]["resume_of"] == orig_id
        assert rec["source"] == "resume"
        assert rec["status"] == "success"
        # 快照步 + 重跑步都在时间线上 (n1 快照, n2/n3 重执行)
        assert [s["node_id"] for s in rec["steps"]] == ["n1", "n2", "n3"]

    def test_resume_from_never_executed_node_full_rerun(self, client):
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rv226b"}, headers=_headers())
        orig_id = r.json()["run_id"]
        _wait_terminal(tc, orig_id)
        # 未知节点 → 400; 已存在但未执行的场景用终态 run 的首节点 → 快照 0 全量重跑
        r = tc.post(f"/api/runs/{orig_id}/resume-from", json={"node_id": "n1"}, headers=_headers())
        assert r.status_code == 200, r.text
        assert r.json()["snapshot_steps"] == 0
        rec = _wait_terminal(tc, r.json()["run_id"])
        assert rec["status"] == "success" and len(rec["steps"]) == 3

    def test_resume_from_unknown_node_400(self, client):
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rv226c"}, headers=_headers())
        orig_id = r.json()["run_id"]
        _wait_terminal(tc, orig_id)
        r = tc.post(f"/api/runs/{orig_id}/resume-from", json={"node_id": "ghost"}, headers=_headers())
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "unknown_node"

    def test_resume_from_missing_node_id_400(self, client):
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rv226d"}, headers=_headers())
        orig_id = r.json()["run_id"]
        _wait_terminal(tc, orig_id)
        r = tc.post(f"/api/runs/{orig_id}/resume-from", json={}, headers=_headers())
        assert r.status_code == 400

    def test_resume_from_not_terminal_409(self, client):
        tc, app = client
        # 种子 running run — 不允许重跑
        registry = app.state.run_registry
        registry._runs["run_226_live"] = {
            "run_id": "run_226_live",
            "workflow_id": "wf-226",
            "workflow_name": "wf-226",
            "status": "running",
            "created_at": 1.0,
            "finished_at": None,
            "steps": [],
            "result": None,
            "error": None,
            "workflow_def": WF_DEF,
        }
        r = tc.post("/api/runs/run_226_live/resume-from", json={"node_id": "n1"}, headers=_headers())
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "not_terminal"

    def test_resume_from_missing_run_404(self, client):
        tc, app = client
        r = tc.post("/api/runs/run_missing/resume-from", json={"node_id": "n1"}, headers=_headers())
        assert r.status_code == 404

    def test_resume_from_dict_nodes_template_shape(self, client):
        # issue #410: template 来源 run 的 workflow_def.nodes 是 dict (node_id -> node),
        # resume-from 曾经 AttributeError 500 — 两种形态都必须可重跑
        tc, app = client
        dict_wf = {
            "name": "wf-226-dict",
            "nodes": {
                "n1": {"id": "n1", "name": "merge", "config": {}},
                "n2": {"id": "n2", "name": "merge", "config": {}},
                "n3": {"id": "n3", "name": "merge", "config": {}},
            },
            "edges": [
                {"source_id": "n1", "target_id": "n2"},
                {"source_id": "n2", "target_id": "n3"},
            ],
        }
        r = tc.post("/api/runs", json={"workflow": dict_wf, "name": "rv226dict"}, headers=_headers())
        assert r.status_code == 202, r.text
        orig_id = r.json()["run_id"]
        orig = _wait_terminal(tc, orig_id)
        assert orig["status"] == "success" and len(orig["steps"]) == 3
        r = tc.post(f"/api/runs/{orig_id}/resume-from", json={"node_id": "n2"}, headers=_headers())
        assert r.status_code == 200, r.text
        rec = _wait_terminal(tc, r.json()["run_id"])
        assert rec["status"] == "success"
        assert [s["node_id"] for s in rec["steps"]] == ["n1", "n2", "n3"]
