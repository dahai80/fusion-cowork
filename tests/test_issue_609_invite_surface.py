# issue #609: invite token HTTP surface — GET peek + POST single-use consume
# for the fusion-site /invite/[token] activation page (fusion-site#149).
# Contract: peek 200 with order summary for a live token, 404 for
# unknown/used/expired; consume is single-use (second POST 404); both routes
# behind the existing bearer key auth; token never logged in plaintext
# (prefix-only tag) and stored hashed (store contract from #597).

from __future__ import annotations

import asyncio
import hashlib

import pytest

STATIC_KEY = "fc-web-test-609"
SERVICE_KEY = "fc-svc-test-609"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setenv("FUSION_COWORK_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("FUSION_COWORK_WORKSPACE_ROOT", str(tmp_path))
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
        delivery_db_path=str(tmp_path / "delivery.db"),
    )
    yield TestClient(app), app, tmp_path


def _auth(key: str = STATIC_KEY):
    return {"Authorization": f"Bearer {key}"}


def _seed_order(tmp_path) -> tuple:
    from fusion_cowork.delivery.store import OrderStore

    async def _go():
        store = OrderStore(str(tmp_path / "delivery.db"))
        await store.connect()
        try:
            order = await store.create_order(1, "rag-qa", "RAG delivery", 2000)
            await store.set_repo(order["order_no"], "dahai80/fusion-cowork")
            token = await store.create_invite(order["order_no"], ttl_hours=72)
            return order, token
        finally:
            await store.close()

    return asyncio.run(_go())


def _stored_hashes(tmp_path) -> list:
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "delivery.db"))
    try:
        rows = conn.execute("SELECT token_hash FROM invite_tokens").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


class TestInvitePeek:
    def test_peek_live_token_returns_order_summary(self, client):
        tc, _, tmp_path = client
        order, token = _seed_order(tmp_path)
        r = tc.get(f"/api/delivery/invites/{token}", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["token_status"] == "active"
        assert body["ttl_hours"] == 72
        assert "consumed" not in body
        assert body["order"]["order_no"] == order["order_no"]
        assert body["order"]["title"] == "RAG delivery"
        assert body["order"]["stage"] == "created"
        assert body["order"]["repo"] == "dahai80/fusion-cowork"
        assert "expires_at" in body
        # peek does not consume
        r2 = tc.get(f"/api/delivery/invites/{token}", headers=_auth())
        assert r2.status_code == 200

    def test_peek_unknown_token_404(self, client):
        tc, _, _ = client
        r = tc.get("/api/delivery/invites/no-such-token", headers=_auth())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "unknown_token"

    def test_peek_consumed_token_404(self, client):
        tc, _, tmp_path = client
        _, token = _seed_order(tmp_path)
        assert tc.post(f"/api/delivery/invites/{token}/consume", headers=_auth()).status_code == 200
        r = tc.get(f"/api/delivery/invites/{token}", headers=_auth())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "token_unavailable"

    def test_peek_expired_token_404(self, client, tmp_path):
        tc, _, _ = client
        _, token = _seed_order(tmp_path)
        conn = __import__("sqlite3").connect(str(tmp_path / "delivery.db"))
        try:
            conn.execute(
                "UPDATE invite_tokens SET expires_at=0 WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
            conn.commit()
        finally:
            conn.close()
        r = tc.get(f"/api/delivery/invites/{token}", headers=_auth())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "token_unavailable"

    def test_peek_requires_auth(self, client):
        tc, _, tmp_path = client
        _, token = _seed_order(tmp_path)
        assert tc.get(f"/api/delivery/invites/{token}").status_code == 401


class TestInviteConsume:
    def test_consume_single_use_second_post_404(self, client):
        tc, _, tmp_path = client
        order, token = _seed_order(tmp_path)
        r = tc.post(f"/api/delivery/invites/{token}/consume", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["consumed"] is True
        assert body["token_status"] == "active"
        assert body["order"]["order_no"] == order["order_no"]
        r2 = tc.post(f"/api/delivery/invites/{token}/consume", headers=_auth())
        assert r2.status_code == 404
        assert r2.json()["detail"]["code"] == "token_unavailable"

    def test_consume_unknown_token_404(self, client):
        tc, _, _ = client
        r = tc.post("/api/delivery/invites/no-such-token/consume", headers=_auth())
        assert r.status_code == 404

    def test_consume_requires_auth(self, client):
        tc, _, _ = client
        assert tc.post("/api/delivery/invites/no-such-token/consume").status_code == 401

    def test_token_stored_hashed_only(self, client):
        tc, _, tmp_path = client
        _, token = _seed_order(tmp_path)
        tc.post(f"/api/delivery/invites/{token}/consume", headers=_auth())
        hashes = _stored_hashes(tmp_path)
        assert hashes == [hashlib.sha256(token.encode()).hexdigest()]
        assert token not in " ".join(hashes)


class TestServiceKeyScope:
    def test_service_key_accepted_on_invite_routes(self, client):
        tc, _, tmp_path = client
        _, token = _seed_order(tmp_path)
        r = tc.get(f"/api/delivery/invites/{token}", headers=_auth(SERVICE_KEY))
        assert r.status_code == 200
        r2 = tc.post(f"/api/delivery/invites/{token}/consume", headers=_auth(SERVICE_KEY))
        assert r2.status_code == 200

    def test_service_key_still_scoped_elsewhere(self, client):
        tc, _, _ = client
        r = tc.get("/api/delivery/orders", headers=_auth(SERVICE_KEY))
        assert r.status_code == 403
        r2 = tc.get("/api/workflows", headers=_auth(SERVICE_KEY))
        assert r2.status_code == 403


class TestTokenLoggingHygiene:
    def test_log_never_contains_plaintext_token(self, client, caplog):
        import logging

        tc, _, tmp_path = client
        _, token = _seed_order(tmp_path)
        with caplog.at_level(logging.INFO, logger="fusion_cowork.server.web_api"):
            tc.get(f"/api/delivery/invites/{token}", headers=_auth())
            tc.post(f"/api/delivery/invites/{token}/consume", headers=_auth())
        for rec in caplog.records:
            assert token not in rec.getMessage()
