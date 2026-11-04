from fastapi.testclient import TestClient

from fusion_cowork.server import web_api


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k278")
    app = web_api.create_web_app(dist_dir=tmp_path / "d", store_root=tmp_path / "s", space_data_dir=tmp_path / "sp")
    return TestClient(app)


class TestSpeechConfig:
    def test_default_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FUSION_SPEECH_PORT", raising=False)
        monkeypatch.delenv("FUSION_SPEECH_WS_URL", raising=False)
        monkeypatch.delenv("FUSION_SPEECH_INGRESS_TOKEN", raising=False)
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.get("/api/speech/config", headers={"x-api-key": "k278"})
        assert r.status_code == 200
        data = r.json()
        assert data["url"] == "ws://127.0.0.1:11465/stream/audio"
        assert data["token"] == ""
        assert data["ingress_enabled"] is True

    def test_env_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_SPEECH_WS_URL", "ws://127.0.0.1:9999/stream/audio")
        monkeypatch.setenv("FUSION_SPEECH_INGRESS_TOKEN", "tok278")
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.get("/api/speech/config", headers={"x-api-key": "k278"})
        data = r.json()
        assert data["url"] == "ws://127.0.0.1:9999/stream/audio"
        assert data["token"] == "tok278"


class TestSpeechHealth:
    def test_down_daemon_reports_status_down(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_SPEECH_PORT", "1")
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.get("/api/speech/health", headers={"x-api-key": "k278"})
        assert r.status_code == 200
        assert r.json()["status"] == "down"

    def test_up_daemon_proxies_payload(self, monkeypatch, tmp_path):
        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"status": "ok", "service": "fusion-speech", "version": "9.9"}

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                assert url.endswith("/health")
                return FakeResp()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = tc.get("/api/speech/health", headers={"x-api-key": "k278"})
        assert r.status_code == 200
        assert r.json()["service"] == "fusion-speech"
