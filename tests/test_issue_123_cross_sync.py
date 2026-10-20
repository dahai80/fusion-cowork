"""#123: /api/deliver 不再永久 503 — cross_sync 经 cross_sync_from_env() 注入 web_serve。

Contract:
- FUSION_COWORK_CROSS_SYNC 未设 → cross_sync_from_env() 返回 None (默认 OFF 零行为变化);
- =1 → CrossDeviceSync 实例, env 参数生效 (host/port/token);
- create_web_app(cross_sync=实例) → POST /api/deliver 真实投递 (fake send_message)。
"""

from __future__ import annotations

import pytest

STATIC_KEY = "fc-web-test-123"


class TestCrossSyncFromEnv:
    def test_disabled_by_default(self, monkeypatch):
        from fusion_cowork.server.sync import cross_sync_from_env

        monkeypatch.delenv("FUSION_COWORK_CROSS_SYNC", raising=False)
        assert cross_sync_from_env() is None

    def test_enabled_builds_engine_with_env_params(self, monkeypatch):
        from fusion_cowork.server.sync import cross_sync_from_env

        monkeypatch.setenv("FUSION_COWORK_CROSS_SYNC", "1")
        monkeypatch.setenv("FUSION_COWORK_SYNC_HOST", "0.0.0.0")
        monkeypatch.setenv("FUSION_COWORK_SYNC_PORT", "12437")
        monkeypatch.setenv("FUSION_COWORK_SYNC_TOKEN", "tok-1")
        engine = cross_sync_from_env()
        assert engine is not None
        assert engine.host == "0.0.0.0"
        assert engine.port == 12437
        assert engine.token == "tok-1"


class TestDeliverInjected:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        web_api._WEB_DIR = tmp_path / "web"
        sent = []

        class FakeCrossSync:
            async def send_message(self, msg):
                sent.append(msg)
                return True

        app = web_api.create_web_app(
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "workflows",
            cross_sync=FakeCrossSync(),
        )
        app.state.run_registry._runs["wf-1"] = {
            "run_id": "wf-1",
            "workflow_name": "t",
            "status": "success",
            "created_at": 0,
            "finished_at": None,
        }
        return TestClient(app), sent

    def _auth(self):
        return {"Authorization": f"Bearer {STATIC_KEY}"}

    def test_deliver_uses_injected_engine(self, client):
        tc, sent = client
        resp = tc.post("/api/deliver", headers=self._auth(), json={"run_id": "wf-1", "target_device_id": "dev-9"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["delivered"] is True
        assert body["receiver"] == "dev-9"
        assert len(sent) == 1
        assert sent[0].payload["run_id"] == "wf-1"

    def test_deliver_unknown_run_404(self, client):
        tc, _ = client
        resp = tc.post("/api/deliver", headers=self._auth(), json={"run_id": "nope"})
        assert resp.status_code == 404
