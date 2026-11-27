# issue #605: POST /api/demand/leads direct-ingest (fusion-site#141 E1) —
# source="direct" structured intake payloads, dedup on idx_leads_external
# (201 new / 200 duplicate), rule spam filter from the normalize stage, same
# match_lead scoring as mailbox leads, zero LLM calls in the web plane.

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
            {
                "id": "doc-extraction",
                "enabled": True,
                "price_cny": 1800,
                "keywords": ["extraction", "抽取"],  # i18n: user-facing
                "skills": ["ocr", "parse"],
            },
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class TestDirectIngest:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k605")
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
        auth = {"Authorization": "Bearer k605"}
        with TestClient(app) as client:
            yield client, auth, db_path

    def _post(self, client, auth, **over):
        body = {
            "source": "direct",
            "external_id": "flow-1001",
            "title": "Build a RAG knowledge base",
            "description": "Need a local RAG QA system with document ingestion and retrieval skills.",
            "budget_hint": "¥2000-3000",
            "skills": ["rag", "retrieval"],
            "client_meta": {"origin_url": "https://site.example/factory/new", "contact": "a@b.c"},
        }
        body.update(over)
        return client.post("/api/demand/leads", json=body, headers=auth)

    def test_ingest_new_201_matched(self, api):
        client, auth, _ = api
        r = self._post(client, auth)
        assert r.status_code == 201
        data = r.json()
        assert data["duplicate"] is False
        assert data["lead_id"] > 0
        assert data["status"] in ("matched", "parked")
        assert "rag-qa" in data["matched_capabilities"]
        # visible in the same inbox the mailbox channel feeds
        inbox = client.get("/api/demand/leads", headers=auth).json()
        assert [ld["id"] for ld in inbox["leads"]] == [data["lead_id"]]
        assert inbox["leads"][0]["source"] == "direct"

    def test_ingest_duplicate_200_same_lead(self, api):
        client, auth, _ = api
        r1 = self._post(client, auth)
        assert r1.status_code == 201
        r2 = self._post(client, auth, title="changed title on retry")
        assert r2.status_code == 200
        assert r2.json()["duplicate"] is True
        assert r2.json()["lead_id"] == r1.json()["lead_id"]
        # retried payload must not mutate the stored lead
        detail = client.get(f"/api/demand/leads/{r1.json()['lead_id']}", headers=auth).json()
        assert detail["title"] == "Build a RAG knowledge base"

    def test_ingest_validations_400(self, api):
        client, auth, _ = api
        assert self._post(client, auth, source="email").status_code == 400
        assert self._post(client, auth, external_id="").status_code == 400
        assert self._post(client, auth, title="").status_code == 400
        assert self._post(client, auth, description="").status_code == 400
        assert client.get("/api/demand/leads", headers=auth).json()["count"] == 0

    def test_ingest_spam_marked_not_matched(self, api):
        client, auth, _ = api
        spam_body = "unsubscribe now — promotion newsletter lottery winner, seo services offer inside"
        r = self._post(client, auth, title="win", description=spam_body, external_id="flow-spam")
        assert r.status_code == 201
        assert r.json()["status"] == "spam"
        assert r.json()["matched_capabilities"] == []

    def test_ingest_skills_and_meta_persisted(self, api):
        client, auth, db_path = api
        r = self._post(client, auth)
        lead_id = r.json()["lead_id"]

        async def read_row():
            from fusion_cowork.demand.store import LeadStore

            store = LeadStore(db_path)
            await store.connect()
            try:
                async with store.db.execute(
                    "SELECT skills, client_meta, extraction FROM leads WHERE id=?", (lead_id,)
                ) as cur:
                    return await cur.fetchone()
            finally:
                await store.close()

        row = asyncio.run(read_row())
        assert json.loads(row[0]) == ["rag", "retrieval"]
        assert json.loads(row[1])["origin_url"].startswith("https://")
        assert row[2] == "rules"  # zero LLM calls in the web plane

    def test_ingest_requires_auth(self, api):
        client, auth, _ = api
        assert self._post(client, {"Authorization": "Bearer wrong"}).status_code == 401
