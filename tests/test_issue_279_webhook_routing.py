import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from fusion_cowork.server import web_api

SECRET = "s279"


class FakePermissionManager:
    def __init__(self):
        self.pending = {"real-run": {"action_id": "act-1"}}

    def find_pending_by_execution(self, execution_id):
        return self.pending.get(execution_id)


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k279")
    monkeypatch.setenv("FUSION_COWORK_WEBHOOK_SECRET", SECRET)
    app = web_api.create_web_app(
        dist_dir=tmp_path / "d",
        store_root=tmp_path / "s",
        space_data_dir=tmp_path / "sp",
        permission_manager=FakePermissionManager(),
    )
    return TestClient(app)


def _post(tc, run_id, payload):
    body = json.dumps(payload).encode()
    return tc.post(
        f"/api/hooks/webhook/{run_id}",
        content=body,
        headers={"X-Fusion-Signature": _sig(body), "Content-Type": "application/json"},
    )


class TestWebhookBodyRouting:
    def test_placeholder_path_routes_via_params_run_id(self, monkeypatch, tmp_path):
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = _post(tc, "RUN_ID", {"approved": True, "params": {"run_id": "real-run"}})
        assert r.status_code == 200
        data = r.json()
        assert data["run_id"] == "real-run"
        assert "reason" not in data

    def test_placeholder_path_routes_via_top_level_run_id(self, monkeypatch, tmp_path):
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = _post(tc, "RUN_ID", {"approved": True, "run_id": "real-run"})
        assert r.status_code == 200
        data = r.json()
        assert data["run_id"] == "real-run"
        assert "reason" not in data

    def test_placeholder_without_match_reports_no_pending(self, monkeypatch, tmp_path):
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = _post(tc, "RUN_ID", {"approved": True, "params": {"run_id": "ghost"}})
        assert r.status_code == 200
        assert r.json()["resumed"] is False
        assert "reason" in r.json()

    def test_path_match_still_wins(self, monkeypatch, tmp_path):
        tc = _client(monkeypatch, tmp_path)
        with tc:
            r = _post(tc, "real-run", {"approved": True})
        assert r.status_code == 200
        data = r.json()
        assert data["run_id"] == "real-run"
        assert "reason" not in data
