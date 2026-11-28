# issue #645: per-lead draft trigger — POST /api/demand/leads/{lead_id}/draft
# runs match (if needed) + dual_draft for exactly one lead, zero mailbox
# involvement, so the drafted->approve path is exercisable on demand.

import asyncio
import json

import pytest


def _catalog(path):
    data = {
        "version": "1",
        "capabilities": [
            {
                "id": "rag-qa",
                "enabled": True,
                "price_cny": 2000,
                "keywords": ["rag", "知识库"],  # i18n: user-facing
                "skills": ["retrieval", "rag"],
            },
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class StubLocal:
    async def list_models(self):
        return []

    async def chat(self, model, messages, **kw):
        class _R:
            content = "LOCAL DRAFT A"

        return _R()


class StubCloud:
    model = "glm-5"

    def __init__(self, *a, **k):
        pass

    async def chat(self, model, messages, **kw):
        class _R:
            content = "CLOUD DRAFT B"

        return _R()

    async def close(self):
        pass


def _patch_clients(monkeypatch):
    monkeypatch.setattr("fusion_cowork.ai.mlx_client.FusionMLXClient", lambda: StubLocal())
    monkeypatch.setattr("fusion_cowork.demand.draft.CloudGLMClient", StubCloud)


def _rewind_lead(db_path: str, lead_id: int, **fields):
    """Push a lead back to an unmatched state to exercise the match-on-draft arm."""

    async def run():
        from fusion_cowork.demand.store import LeadStore

        store = LeadStore(db_path)
        await store.connect()
        try:
            base = {"status": "new", "catalog_version": "", "matched_capabilities": [], "match_score": 0.0}
            base.update(fields)
            await store.update_lead(lead_id, base)
        finally:
            await store.close()

    asyncio.run(run())


class TestPerLeadDraft:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k645")
        from fusion_cowork.engine import PermissionManager, WorkflowEngine

        db_path = str(tmp_path / "demand.db")
        eng = WorkflowEngine(permission_manager=PermissionManager())
        from fusion_cowork.server import web_api

        app = web_api.create_web_app(
            engine=eng,
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "store",
            demand_db_path=db_path,
            demand_catalog_path=_catalog(tmp_path / "catalog.json"),
        )
        auth = {"Authorization": "Bearer k645"}
        with TestClient(app) as client:
            yield client, auth, db_path

    def _ingest(self, client, auth, external_id="645-1"):
        r = client.post(
            "/api/demand/leads",
            json={
                "source": "direct",
                "external_id": external_id,
                "title": "Build a RAG knowledge base",
                "description": "Need a local RAG QA system with retrieval skills.",
                "skills": ["rag"],
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        return r.json()["lead_id"]

    def test_draft_matched_lead_200(self, api, monkeypatch):
        _patch_clients(monkeypatch)
        client, auth, _ = api
        lead_id = self._ingest(client, auth)
        r = client.post(f"/api/demand/leads/{lead_id}/draft", json={}, headers=auth)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "drafted"
        assert data["draft_a"] == "LOCAL DRAFT A"
        assert data["draft_b"] == "CLOUD DRAFT B"
        assert data["draft_meta"]["model_b"] == "glm-5"

    def test_draft_404(self, api):
        client, auth, _ = api
        assert client.post("/api/demand/leads/9999/draft", json={}, headers=auth).status_code == 404

    def test_draft_spam_409(self, api):
        client, auth, _ = api
        lead_id = self._ingest(client, auth, external_id="645-spam")
        _rewind_lead(api[2], lead_id, status="spam")
        r = client.post(f"/api/demand/leads/{lead_id}/draft", json={}, headers=auth)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "invalid_status"

    def test_draft_idempotent_when_already_drafted(self, api, monkeypatch):
        _patch_clients(monkeypatch)
        client, auth, _ = api
        lead_id = self._ingest(client, auth)
        first = client.post(f"/api/demand/leads/{lead_id}/draft", json={}, headers=auth)
        assert first.status_code == 200
        second = client.post(f"/api/demand/leads/{lead_id}/draft", json={}, headers=auth)
        assert second.status_code == 200
        assert second.json()["draft_a"] == first.json()["draft_a"]

    def test_draft_runs_match_when_unscored(self, api, monkeypatch):
        _patch_clients(monkeypatch)
        client, auth, db_path = api
        lead_id = self._ingest(client, auth)
        _rewind_lead(db_path, lead_id)
        r = client.post(f"/api/demand/leads/{lead_id}/draft", json={}, headers=auth)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "drafted"
        assert "rag-qa" in data["matched_capabilities"]
        assert data["catalog_version"] == "1"

    def test_draft_parked_409_then_force(self, api, monkeypatch):
        _patch_clients(monkeypatch)
        client, auth, db_path = api
        lead_id = self._ingest(client, auth)
        _rewind_lead(db_path, lead_id)
        r = client.post(f"/api/demand/leads/{lead_id}/draft", json={"threshold": 99}, headers=auth)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "match_parked"
        r2 = client.post(f"/api/demand/leads/{lead_id}/draft", json={"threshold": 99, "force": True}, headers=auth)
        assert r2.status_code == 200
        assert r2.json()["status"] == "drafted"

    def test_draft_then_approve_metric_path(self, api, monkeypatch):
        # the point of #645: approve requires status=drafted — the operator
        # trigger makes the edit-distance metric exercisable without a mailbox
        _patch_clients(monkeypatch)
        client, auth, _ = api
        lead_id = self._ingest(client, auth)
        assert client.post(f"/api/demand/leads/{lead_id}/draft", json={}, headers=auth).status_code == 200
        r = client.post(
            f"/api/demand/leads/{lead_id}/approve",
            json={"pick": "b", "text": "CLOUD DRAFT B edited"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "approved"
        assert data["draft_pick"] == "b"
        assert data["draft_edit_distance"] > 0

    def test_draft_requires_auth(self, api):
        client, auth, _ = api
        lead_id = self._ingest(client, auth)
        r = client.post(f"/api/demand/leads/{lead_id}/draft", json={}, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
