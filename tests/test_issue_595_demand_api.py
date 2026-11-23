# issue #595: demand engine web API — leads list/detail/approve/discard,
# metrics proxy, catalog get/put with parked re-scoring, IMAP settings
# (password write-only, env file 0600, read-only EXAMINE test).

import asyncio
import json
import stat
import time

import pytest


def _lead(**over):
    base = {
        "message_id": f"m-{time.time_ns()}",
        "source": "qq_imap",
        "external_id": "",
        "title": "RAG bot for support docs",
        "description": "need rag knowledge base over manuals",
        "budget_hint": "3000 CNY",
        "skills": ["rag"],
        "arrived_at": time.time(),
        "status": "drafted",
    }
    base.update(over)
    return base


_DRAFT_FIELDS = {
    "draft_a": "draft A text",
    "draft_b": "draft B text",
    "draft_meta": {"model_a": "qwen", "model_b": "glm-5"},
    "matched_capabilities": ["rag-qa"],
    "match_score": 5.0,
    "catalog_version": "1",
}


def _seed_all(db_path):
    # one asyncio.run block — aiosqlite connection lives and dies on the same
    # loop (asyncio.get_event_loop() is unusable in full-suite MainThread)
    async def main():
        from fusion_cowork.demand.store import LeadStore

        store = LeadStore(db_path)
        await store.connect()
        try:
            drafted = await store.insert_lead(_lead())
            await store.update_lead(drafted, _DRAFT_FIELDS)
            normalized = await store.insert_lead(_lead(status="normalized"))
            parked = await store.insert_lead(_lead(status="parked", title="parked one"))
            await store.update_lead(parked, _DRAFT_FIELDS)
            return drafted, normalized, parked
        finally:
            await store.close()

    return asyncio.run(main())


class TestDemandAPI:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k595")
        monkeypatch.setenv("FUSION_COWORK_DEMAND_IMAP_ENV", str(tmp_path / "imap.env"))
        monkeypatch.delenv("FUSION_QQ_IMAP_USER", raising=False)
        monkeypatch.delenv("FUSION_QQ_IMAP_PASSWORD", raising=False)
        from fusion_cowork.engine import PermissionManager, WorkflowEngine

        db_path = str(tmp_path / "demand.db")
        drafted_id, normalized_id, parked_id = _seed_all(db_path)

        eng = WorkflowEngine(permission_manager=PermissionManager())
        from fusion_cowork.server import web_api

        app = web_api.create_web_app(
            engine=eng,
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "store",
            demand_db_path=db_path,
            demand_catalog_path=str(tmp_path / "catalog.json"),
        )
        auth = {"Authorization": "Bearer k595"}
        with TestClient(app) as client:
            yield (
                client,
                auth,
                {
                    "drafted": drafted_id,
                    "normalized": normalized_id,
                    "parked": parked_id,
                    "db": db_path,
                    "catalog": str(tmp_path / "catalog.json"),
                },
            )

    # ── leads ──

    def test_leads_list_and_filter(self, api):
        client, auth, ids = api
        all_leads = client.get("/api/demand/leads", headers=auth).json()
        assert all_leads["count"] == 3
        assert [ld["id"] for ld in all_leads["leads"]] == sorted([ld["id"] for ld in all_leads["leads"]], reverse=True)
        only = client.get("/api/demand/leads", params={"status": "drafted"}, headers=auth).json()
        assert {ld["id"] for ld in only["leads"]} == {ids["drafted"]}

    def test_lead_detail_and_404(self, api):
        client, auth, ids = api
        d = client.get(f"/api/demand/leads/{ids['drafted']}", headers=auth).json()
        assert d["draft_a"] == "draft A text"
        assert d["draft_meta"]["model_b"] == "glm-5"
        assert client.get("/api/demand/leads/99999", headers=auth).status_code == 404

    def test_approve_drafted_with_edit(self, api):
        client, auth, ids = api
        r = client.post(
            f"/api/demand/leads/{ids['drafted']}/approve", json={"pick": "a", "text": "edited final text"}, headers=auth
        ).json()
        assert r["status"] == "approved"
        assert r["draft_pick"] == "a"
        assert r["draft_final"] == "edited final text"
        assert 0 < r["draft_edit_distance"] < 1  # differs from draft A

    def test_approve_non_drafted_409(self, api):
        client, auth, ids = api
        r = client.post(f"/api/demand/leads/{ids['normalized']}/approve", json={"pick": "a"}, headers=auth)
        assert r.status_code == 409

    def test_approve_bad_pick_400(self, api):
        client, auth, ids = api
        assert (
            client.post(f"/api/demand/leads/{ids['drafted']}/approve", json={"pick": "z"}, headers=auth).status_code
            == 400
        )

    def test_discard_with_reason(self, api):
        client, auth, ids = api
        r = client.post(
            f"/api/demand/leads/{ids['parked']}/discard", json={"reason": "out of scope"}, headers=auth
        ).json()
        assert r["status"] == "discarded"
        assert r["discard_reason"] == "out of scope"

    # ── metrics ──

    def test_metrics_proxy(self, api):
        client, auth, ids = api
        client.post(f"/api/demand/leads/{ids['drafted']}/approve", json={"pick": "b"}, headers=auth)
        m = client.get("/api/demand/metrics", headers=auth).json()
        assert m["total_picks"] == 1
        assert m["pick_rate_cloud"] == 1.0
        assert "per_capability_win_rate" in m

    # ── catalog ──

    def test_catalog_get_seed_fallback(self, api, tmp_path):
        client, auth, ids = api
        c = client.get("/api/demand/catalog", headers=auth).json()
        assert c["version"] == "1"  # seed (runtime file absent)
        assert len(c["capabilities"]) >= 4

    def test_catalog_put_bumps_and_rescores_parked(self, api):
        client, auth, ids = api
        c = client.get("/api/demand/catalog", headers=auth).json()
        body = {**c, "version": "2"}
        r = client.put("/api/demand/catalog", json=body, headers=auth).json()
        assert r["version"] == "2"
        assert r["previous_version"] == "1"
        assert r["parked_re_scored"] >= 1
        # parked lead re-scored against v2 (still parked or matched, version stamped)
        d = client.get(f"/api/demand/leads/{ids['parked']}", headers=auth).json()
        assert d["catalog_version"] == "2"
        # file persisted
        saved = json.loads(open(ids["catalog"]).read())
        assert saved["version"] == "2"

    def test_catalog_put_same_version_autobumps(self, api):
        client, auth, ids = api
        c = client.get("/api/demand/catalog", headers=auth).json()
        client.put("/api/demand/catalog", json={**c, "version": "2"}, headers=auth)
        r2 = client.put("/api/demand/catalog", json={**c, "version": "2"}, headers=auth).json()
        assert r2["version"] == "2.1"

    def test_catalog_put_invalid_400(self, api):
        client, auth, ids = api
        assert client.put("/api/demand/catalog", json={"version": "9"}, headers=auth).status_code == 400
        assert client.put("/api/demand/catalog", json={"capabilities": [{"id": "x"}]}, headers=auth).status_code == 400

    # ── settings ──

    def test_settings_get_never_contains_password(self, api):
        client, auth, ids = api
        s = client.get("/api/demand/settings", headers=auth).json()
        assert "password" not in s
        assert s["password_present"] is False
        assert s["cursor"] == 0
        assert "env_path" in s

    def test_settings_put_writes_0600_and_keeps_password(self, api, tmp_path):
        client, auth, ids = api
        env = tmp_path / "imap.env"
        r = client.put(
            "/api/demand/settings",
            json={"host": "imap.qq.com", "port": 993, "user": "u@qq.com", "password": "sec", "folder": "INBOX"},
            headers=auth,
        ).json()
        assert r["saved"] is True
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        # second write without password keeps the stored one
        client.put("/api/demand/settings", json={"host": "imap.qq.com", "port": 993, "user": "u@qq.com"}, headers=auth)
        content = env.read_text()
        assert "FUSION_QQ_IMAP_PASSWORD=sec" in content
        s = client.get("/api/demand/settings", headers=auth).json()
        assert s["password_present"] is True
        assert "sec" not in json.dumps(s)

    def test_settings_test_no_creds_fails_clean(self, api):
        client, auth, ids = api
        r = client.post("/api/demand/settings/test", headers=auth).json()
        assert r["ok"] is False
        assert "error" in r
