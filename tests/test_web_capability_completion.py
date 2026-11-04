"""Web capability completion: permission rules / benchmark / system info+clean / session fork+cleanup / db backup routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fusion_cowork.engine.node import NodeRegistry, NodeResult, NodeStatus
from fusion_cowork.engine.permission import PermissionLevel
from fusion_cowork.engine.permission_manager import PermissionManager

HDR = {"x-api-key": "k264"}


class MockBenchNode:
    name = "bench_mock_264"


def _client(monkeypatch, tmp_path, pm=None, session_store=None):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k264")
    from fusion_cowork.server.web_api import create_web_app

    app = create_web_app(
        permission_manager=pm or PermissionManager(),
        session_store=session_store,
        space_data_dir=tmp_path / "space",
    )
    return TestClient(app)


class TestPermissionRules:
    def test_rules_roundtrip(self, monkeypatch, tmp_path):
        pm = PermissionManager()
        client = _client(monkeypatch, tmp_path, pm=pm)
        monkeypatch.setattr("fusion_cowork.engine.permission_manager._PERMISSIONS_FILE", str(tmp_path / "p.json"))
        r = client.post(
            "/api/permissions/rules", json={"tool_name": "shell_exec", "allowed": True, "scope": "echo *"}, headers=HDR
        )
        assert r.status_code == 201
        got = client.get("/api/permissions/rules", headers=HDR).json()
        assert got["level"] == PermissionLevel.CONFIRM.value
        assert any(x["tool_name"] == "shell_exec" and x["allowed"] for x in got["rules"])
        d = client.delete("/api/permissions/rules/shell_exec", headers=HDR)
        assert d.json()["removed"] >= 1
        assert client.delete("/api/permissions/rules/shell_exec", headers=HDR).json()["removed"] == 0

    def test_rule_requires_tool_name(self, monkeypatch, tmp_path):
        client = _client(monkeypatch, tmp_path)
        assert client.post("/api/permissions/rules", json={}, headers=HDR).status_code == 400

    def test_level_set_and_400(self, monkeypatch, tmp_path):
        pm = PermissionManager()
        client = _client(monkeypatch, tmp_path, pm=pm)
        r = client.put("/api/permissions/level", json={"level": "auto"}, headers=HDR)
        assert r.status_code == 200 and r.json()["level"] == "auto"
        assert pm.level == PermissionLevel.AUTO
        assert client.put("/api/permissions/level", json={"level": "nope"}, headers=HDR).status_code == 400


class TestBenchmark:
    @pytest.mark.asyncio
    async def test_run_and_status(self, monkeypatch, tmp_path):
        import asyncio

        from fusion_cowork.benchmark.runner import BenchmarkRunner

        class N:
            name = "bench_node_264"

            async def execute(self, _):
                return NodeResult(status=NodeStatus.SUCCESS, data={"ok": 1})

        monkeypatch.setitem(NodeRegistry._registry, "bench_node_264", N)
        calls = {"n": 0}

        async def fake_run_nodes(self, specs):
            calls["n"] += 1
            await asyncio.sleep(0.01)
            from fusion_cowork.benchmark.runner import BenchmarkResult

            return [BenchmarkResult(node_name=s["node"], status="success", latency_ms=10.0) for s in specs]

        monkeypatch.setattr(BenchmarkRunner, "run_nodes", fake_run_nodes)
        client = _client(monkeypatch, tmp_path)
        r = client.post(
            "/api/benchmark/run",
            json={"specs": [{"node": "bench_node_264", "params": {}}], "repeats": 2, "wait": True},
            headers=HDR,
        )
        assert r.status_code == 200 and r.json()["started"] is True
        for _ in range(100):
            st = client.get("/api/benchmark/status", headers=HDR).json()
            if not st["running"]:
                break
            await asyncio.sleep(0.02)
        assert st["running"] is False and st["error"] == ""
        assert len(st["results"]) == 1

    def test_unknown_node_400(self, monkeypatch, tmp_path):
        client = _client(monkeypatch, tmp_path)
        r = client.post("/api/benchmark/run", json={"specs": [{"node": "nope_264"}]}, headers=HDR)
        assert r.status_code == 400

    def test_empty_specs_400(self, monkeypatch, tmp_path):
        client = _client(monkeypatch, tmp_path)
        assert client.post("/api/benchmark/run", json={"specs": []}, headers=HDR).status_code == 400


class TestSystem:
    def test_info(self, monkeypatch, tmp_path):
        client = _client(monkeypatch, tmp_path)
        r = client.get("/api/system/info", headers=HDR)
        assert r.status_code == 200
        body = r.json()
        assert body["node_count"] > 0
        assert body["version"] and body["python"]

    @pytest.mark.asyncio
    async def test_clean_dry_run(self, monkeypatch, tmp_path):
        from fusion_cowork.nodes.macos import DesktopCleanNode, DownloadOrganizerNode

        async def fake_execute(self, _):
            return NodeResult(status=NodeStatus.SUCCESS, data={"moved": 0}, summary="0 项")

        monkeypatch.setattr(DesktopCleanNode, "execute", fake_execute)
        monkeypatch.setattr(DownloadOrganizerNode, "execute", fake_execute)
        client = _client(monkeypatch, tmp_path)
        r = client.post("/api/system/clean", json={"dry_run": True}, headers=HDR)
        assert r.status_code == 200
        assert r.json()["outputs"]["desktop"] == "0 项"


class TestSessions:
    def test_fork_and_cleanup(self, monkeypatch, tmp_path):
        from fusion_cowork.engine.session import Session, SessionStore

        SessionStore(db_path=str(tmp_path / "s.db"))  # format smoke; route uses FakeStore
        sid = "sess-264"
        forked_id = f"{sid}-fork"

        class FakeStore:
            def fork(self, session_id, from_step=0, tenant_id=None):
                return Session(id=forked_id, workflow_id="wf", status="forked")

            def cleanup_expired(self, expire_days=30, tenant_id=None):
                return 3

        client = _client(monkeypatch, tmp_path, session_store=FakeStore())
        r = client.post(f"/api/sessions/{sid}/fork", json={"from_step": 2}, headers=HDR)
        assert r.status_code == 200 and r.json()["session_id"] == forked_id
        r2 = client.post("/api/sessions/cleanup", json={"days": 7}, headers=HDR)
        assert r2.json() == {"removed": 3, "days": 7}

    def test_fork_missing_404(self, monkeypatch, tmp_path):
        from fusion_cowork.engine.session import SessionStore

        store = SessionStore(db_path=str(tmp_path / "s2.db"))  # real store: ghost id not found
        client = _client(monkeypatch, tmp_path, session_store=store)
        assert client.post("/api/sessions/ghost/fork", json={}, headers=HDR).status_code == 404


class TestDbBackup:
    def test_backup_requires_dsn(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("FUSION_PG_DSN", raising=False)
        client = _client(monkeypatch, tmp_path)
        assert client.post("/api/admin/db/backup", json={}, headers=HDR).status_code == 400

    def test_backup_ok(self, monkeypatch, tmp_path):
        from fusion_cowork.db.backup import BackupManager

        def fake_backup(self, dest):
            from pathlib import Path

            p = Path(dest)
            p.write_bytes(b"x")
            return p

        monkeypatch.setattr(BackupManager, "backup", fake_backup)
        client = _client(monkeypatch, tmp_path)
        r = client.post(
            "/api/admin/db/backup",
            json={"dsn": "postgresql://u:p@localhost:5432/db", "dest": str(tmp_path / "b.sql.gz")},
            headers=HDR,
        )
        assert r.status_code == 200 and r.json()["path"].endswith("b.sql.gz")
