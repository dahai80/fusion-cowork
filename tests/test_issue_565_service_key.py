"""issue #565: service credential path for the storefront bff.

FUSION_COWORK_SERVICE_KEY is a dedicated service-to-service key (not a user
key): accepted alongside the static env key + gateway-issued module key, but
SCOPED to /api/runs* endpoints only — workflows CRUD, spaces, approvals and
admin surface stay user-key territory (403 out of scope).
"""

import pytest
from fastapi.testclient import TestClient

from fusion_cowork.server import web_api

STATIC_KEY = "static-user-key-565"
SERVICE_KEY = "svc-storefront-key-565"

_PROBE_WF = {
    "name": "svc-probe-565",
    "nodes": [{"id": "n1", "name": "file_find", "params": {"patterns": ["*.txt"], "search_path": "/tmp"}}],
    "edges": [],
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setenv("FUSION_COWORK_SERVICE_KEY", SERVICE_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(dist_dir=tmp_path / "no-dist", store_root=tmp_path / "workflows")
    return TestClient(app)


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class TestServiceKeyAuth:
    def test_check_static_key_ids(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        monkeypatch.setenv("FUSION_COWORK_SERVICE_KEY", SERVICE_KEY)
        assert web_api.check_static_key(STATIC_KEY)["key_id"] == "static-env"
        assert web_api.check_static_key(SERVICE_KEY)["key_id"] == "service"
        assert web_api.check_static_key(SERVICE_KEY)["permissions"] == ["runs"]
        assert web_api.check_static_key("wrong") is None

    def test_service_key_absent_env(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_SERVICE_KEY", raising=False)
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        assert web_api.check_static_key(SERVICE_KEY) is None

    def test_auth_configured_with_service_key_only(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        monkeypatch.setenv("FUSION_COWORK_SERVICE_KEY", SERVICE_KEY)
        assert web_api.auth_configured() is True


class TestServiceKeyScope:
    def test_create_run_with_service_key(self, client):
        resp = client.post("/api/runs", headers=_bearer(SERVICE_KEY), json={"workflow": _PROBE_WF})
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]
        assert run_id

    def test_poll_run_with_service_key(self, client):
        run_id = client.post("/api/runs", headers=_bearer(SERVICE_KEY), json={"workflow": _PROBE_WF}).json()["run_id"]
        resp = client.get(f"/api/runs/{run_id}", headers=_bearer(SERVICE_KEY))
        assert resp.status_code == 200
        record = resp.json()
        assert record["run_id"] == run_id
        assert "status" in record

    def test_out_of_scope_workflows_403(self, client):
        resp = client.get("/api/workflows", headers=_bearer(SERVICE_KEY))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    def test_out_of_scope_approvals_403(self, client):
        resp = client.get("/api/permissions/pending", headers=_bearer(SERVICE_KEY))
        assert resp.status_code == 403

    def test_out_of_scope_models_403(self, client):
        resp = client.get("/api/models", headers=_bearer(SERVICE_KEY))
        assert resp.status_code == 403

    def test_resume_from_route_in_scope(self, client):
        # route reachable with the service key (404 unknown run — scope allows
        # the path; the run itself does not exist)
        resp = client.post("/api/runs/nope-565/resume-from", headers=_bearer(SERVICE_KEY), json={"node_id": "n1"})
        assert resp.status_code == 404

    def test_invalid_key_401(self, client):
        assert client.get("/api/runs", headers=_bearer("garbage")).status_code == 401
        assert client.post("/api/runs", json={"workflow": _PROBE_WF}).status_code == 401

    def test_user_key_unaffected(self, client):
        # static user key keeps admin reach on non-run routes (no regression)
        assert client.get("/api/workflows", headers=_bearer(STATIC_KEY)).status_code == 200
        assert client.post("/api/runs", headers=_bearer(STATIC_KEY), json={"workflow": _PROBE_WF}).status_code == 202

    def test_health_open_without_key(self, client):
        assert client.get("/health").status_code == 200
