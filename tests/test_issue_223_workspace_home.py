"""issue #223: workspace-first home — run↔Space 关联 (space_id) + /api/workspaces 聚合 + runs ?space_id 过滤 + v2 持久化迁移。"""

from __future__ import annotations

import sqlite3
import time

import pytest

from fusion_cowork.engine.run_persistence import RunStateStore
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()

STATIC_KEY = "test-key-223"

WF_DEF = {
    "name": "wf-223",
    "nodes": [{"id": "n1", "name": "python_repl", "config": {"code": "1 + 1"}}],
    "edges": [],
}


def _client(monkeypatch, tmp_path, with_space=True):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    kwargs = {"dist_dir": tmp_path / "no-dist", "store_root": tmp_path / "workflows"}
    if with_space:
        kwargs["space_data_dir"] = tmp_path / "spaces"
    app = web_api.create_web_app(**kwargs)
    return TestClient(app), app


def _client_no_space_store(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_api=FastAPI(),
    )
    return TestClient(app), app


def _make_space(tc, name: str) -> str:
    r = tc.post("/api/space/spaces", json={"name": name}, headers={"Authorization": f"Bearer {STATIC_KEY}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


class TestWorkspacesRoute:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        return _client(monkeypatch, tmp_path)

    def _headers(self):
        return {"Authorization": f"Bearer {STATIC_KEY}"}

    def test_available_false_when_space_stack_absent(self, monkeypatch, tmp_path):
        tc, _ = _client_no_space_store(monkeypatch, tmp_path)
        r = tc.get("/api/workspaces", headers=self._headers())
        assert r.status_code == 200
        assert r.json() == {"workspaces": [], "available": False}

    def test_aggregates_spaces_with_counts(self, client, monkeypatch):
        tc, app = client
        sid_a = _make_space(tc, "目录A")
        sid_b = _make_space(tc, "目录B")
        for uid in ("u1", "u2"):
            r = tc.post(
                f"/api/space/spaces/{sid_a}/members",
                json={"user_id": uid, "role": "member"},
                headers=self._headers(),
            )
            assert r.status_code == 201, r.text
        registry = app.state.run_registry
        registry._runs["run_ws_1"] = {
            "run_id": "run_ws_1",
            "workflow_id": "wf-1",
            "workflow_name": "wf",
            "status": "running",
            "created_at": 200.0,
            "finished_at": None,
            "steps": [],
            "result": None,
            "error": None,
            "space_id": sid_a,
        }
        r = tc.get("/api/workspaces", headers=self._headers())
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        by_id = {w["id"]: w for w in body["workspaces"]}
        # 建目录自动登记 owner 成员 + 2 名受邀 = 3
        assert by_id[sid_a]["member_count"] == 3
        assert by_id[sid_a]["task_count"] == 1
        assert by_id[sid_a]["last_run_at"] == 200.0
        assert by_id[sid_b]["member_count"] == 1  # 仅 owner
        assert by_id[sid_b]["task_count"] == 0
        assert by_id[sid_b]["last_run_at"] is None
        assert by_id[sid_a]["name"] == "目录A"

    def test_list_spaces_failure_502(self, client, monkeypatch):
        tc, app = client
        store = app.state.space_store

        async def boom(limit=100, *a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(store, "list_spaces", boom)
        r = tc.get("/api/workspaces", headers=self._headers())
        assert r.status_code == 502
        assert r.json()["detail"]["code"] == "upstream_error"

    def test_member_count_failure_defaults_zero(self, client, monkeypatch):
        tc, app = client
        sid = _make_space(tc, "目录C")
        store = app.state.space_store

        async def boom(space_id, tenant_id=None):
            raise RuntimeError("count failed")

        monkeypatch.setattr(store, "count_members", boom)
        r = tc.get("/api/workspaces", headers=self._headers())
        assert r.status_code == 200
        by_id = {w["id"]: w for w in r.json()["workspaces"]}
        assert by_id[sid]["member_count"] == 0


class TestRunsSpaceId:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        return _client(monkeypatch, tmp_path)

    def _headers(self):
        return {"Authorization": f"Bearer {STATIC_KEY}"}

    def test_runs_create_persists_space_id_and_filters(self, client):
        tc, app = client
        sid = _make_space(tc, "目录D")
        r = tc.post(
            "/api/runs",
            json={"workflow": WF_DEF, "name": "home-run", "space_id": sid, "source": "home"},
            headers=self._headers(),
        )
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        registry = app.state.run_registry
        assert registry.get(run_id)["space_id"] == sid

        r = tc.get(f"/api/runs?space_id={sid}", headers=self._headers())
        assert r.status_code == 200
        ids = [x["run_id"] for x in r.json()["runs"]]
        assert run_id in ids
        assert all(x["space_id"] == sid for x in r.json()["runs"])

        r = tc.get("/api/runs?space_id=nonexistent-space", headers=self._headers())
        assert r.json()["count"] == 0

        r = tc.get("/api/runs", headers=self._headers())
        assert run_id in [x["run_id"] for x in r.json()["runs"]]

    def test_runs_create_rejects_non_string_space_id(self, client):
        tc, _ = client
        r = tc.post(
            "/api/runs",
            json={"workflow": WF_DEF, "space_id": 123},
            headers=self._headers(),
        )
        assert r.status_code == 400
        assert "space_id" in r.json()["detail"]["message"]

    def test_registry_list_default_space_id_empty(self, client):
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF}, headers=self._headers())
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        assert app.state.run_registry.get(run_id)["space_id"] == ""
        item = next(x for x in app.state.run_registry.list() if x["run_id"] == run_id)
        assert item["space_id"] == ""


class TestRunStateStoreSpaceId:
    def test_roundtrip_preserves_space_id(self, tmp_path):
        store = RunStateStore(tmp_path / "run_states.db")
        record = {
            "run_id": "run_223",
            "workflow_id": "wf",
            "workflow_name": "w",
            "status": "paused",
            "created_at": time.time(),
            "workflow_def": WF_DEF,
            "steps": [],
            "directives": [],
            "space_id": "space-xyz",
        }
        store.save(record, paused_reason="manual")
        rows = store.load_active()
        assert len(rows) == 1
        assert rows[0]["space_id"] == "space-xyz"
        store.close()

    def test_v1_migration_adds_space_id_column(self, tmp_path):
        path = tmp_path / "run_states.db"
        # 手工建 v1 库: 无 space_id 列, user_version=1
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE run_states (
                run_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL DEFAULT '',
                workflow_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                paused_reason TEXT NOT NULL DEFAULT '',
                workflow_def TEXT NOT NULL DEFAULT '{}',
                steps TEXT NOT NULL DEFAULT '[]',
                directives TEXT NOT NULL DEFAULT '[]',
                memory INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        store = RunStateStore(path)
        store.save(
            {
                "run_id": "run_v1",
                "status": "running",
                "workflow_def": WF_DEF,
                "space_id": "sp1",
            }
        )
        rows = store.load_active()
        assert rows[0]["space_id"] == "sp1"
        store.close()

    def test_v1_row_without_space_id_defaults_empty(self, tmp_path):
        path = tmp_path / "run_states.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE run_states (
                run_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL DEFAULT '',
                workflow_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                paused_reason TEXT NOT NULL DEFAULT '',
                workflow_def TEXT NOT NULL DEFAULT '{}',
                steps TEXT NOT NULL DEFAULT '[]',
                directives TEXT NOT NULL DEFAULT '[]',
                memory INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );
            INSERT INTO run_states (run_id, status, created_at) VALUES ('run_old', 'paused', 1.0);
            """
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        store = RunStateStore(path)
        rows = store.load_active()
        assert rows[0]["run_id"] == "run_old"
        assert rows[0]["space_id"] == ""
        store.close()


class TestRecoveryPreservesSpaceId:
    async def test_recover_persisted_keeps_space_id(self, tmp_path, monkeypatch):
        from fusion_cowork.engine import PermissionLevel, PermissionManager
        from fusion_cowork.engine.events import EventEmitter
        from fusion_cowork.engine.workflow import WorkflowEngine
        from fusion_cowork.server.web_api import RunRegistry

        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        monkeypatch.delenv("FUSION_MEMORY_API_KEY", raising=False)
        db = tmp_path / "run_states.db"
        store = RunStateStore(db)
        store.save(
            {
                "run_id": "run_rec_223",
                "workflow_id": "wf",
                "workflow_name": "w",
                "status": "paused",
                "created_at": time.time(),
                "workflow_def": WF_DEF,
                "steps": [],
                "directives": [],
                "space_id": "space-rec",
            },
            paused_reason="manual",
        )
        store.close()

        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        emitter = EventEmitter()
        engine = WorkflowEngine(permission_manager=pm, event_emitter=emitter)
        reg = RunRegistry(engine, event_emitter=emitter, run_store=RunStateStore(db))
        assert await reg.recover_persisted() == 1
        rec = reg.get("run_rec_223")
        assert rec is not None
        assert rec["space_id"] == "space-rec"
        assert rec["recovered"] is True
        reg.run_store.close()
