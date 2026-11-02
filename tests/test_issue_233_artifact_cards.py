"""issue #233: artifact 卡片 — 引擎 ref 提取 + 卡片代理路由 + fusion-artifacts-engine v0.6.0 契约消费。"""

from __future__ import annotations

import asyncio

import pytest

from fusion_cowork.nodes import import_all_nodes

import_all_nodes()

STATIC_KEY = "test-key-233"

CARD_PAYLOAD = {
    "id": "art_1",
    "name": "Q3 Report",
    "type": "report",
    "kind": "document",
    "current_version": 3,
    "summary": "十月对账异常项汇总",
    "source_module": "fusion-doc",
    "is_starred": True,
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app), app


def _headers():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestFindArtifactRefs:
    def test_extracts_in_order_with_dedupe(self):
        from fusion_cowork.server.web_api import find_artifact_refs

        text = (
            "结果 [Artifact: B 报告 | ID: b_1 | Version: v2 | Type: report | Size: 4096] "
            "与 [Artifact: A 图表 | ID: a_1 | v1], 重复 [Artifact: B 副本 | ID: b_1 | v3]"
        )
        refs = find_artifact_refs(text)
        assert refs == [
            {"artifact_id": "b_1", "name": "B 报告"},
            {"artifact_id": "a_1", "name": "A 图表"},
        ]

    def test_non_string_or_no_match_returns_empty(self):
        from fusion_cowork.server.web_api import find_artifact_refs

        assert find_artifact_refs(None) == []
        assert find_artifact_refs(42) == []
        assert find_artifact_refs("plain text no refs") == []
        assert find_artifact_refs("[Artifact: missing id pipe]") == []

    def test_ref_text_roundtrip(self):
        # _artifact_ref_text 重建的 ref 仍可被 find_artifact_refs 提取 (卡片注入 composer 后闭环)
        from fusion_cowork.server.web_api import _artifact_ref_text, find_artifact_refs

        card = {
            "name": "Q3 Report",
            "artifact_id": "art_1",
            "current_version": 3,
            "type": "report",
            "summary": "十月对账异常项汇总",
        }
        refs = find_artifact_refs(_artifact_ref_text(card))
        assert refs == [{"artifact_id": "art_1", "name": "Q3 Report"}]


class TestFetchArtifactCard:
    def test_success_flat_payload(self, monkeypatch):
        import httpx

        from fusion_cowork.server import web_api

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json=CARD_PAYLOAD)

        monkeypatch.setenv("FUSION_ARTIFACTS_API_KEY", "sek-233")
        card = asyncio.run(web_api._fetch_artifact_card("art_1", transport=httpx.MockTransport(handler)))
        assert card == {
            "artifact_id": "art_1",
            "name": "Q3 Report",
            "type": "report",
            "kind": "document",
            "current_version": 3,
            "summary": "十月对账异常项汇总",
            "source_module": "fusion-doc",
            "is_starred": True,
            "deep_link": "/shell/fusion-artifacts/#/a/art_1",
        }
        # 契约路径 + Bearer 鉴权
        assert seen["url"].endswith("/api/v1/artifacts/art_1")
        assert seen["auth"] == "Bearer sek-233"

    def test_nested_data_envelope(self, monkeypatch):
        import httpx

        from fusion_cowork.server import web_api

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": CARD_PAYLOAD})

        card = asyncio.run(web_api._fetch_artifact_card("art_1", transport=httpx.MockTransport(handler)))
        assert card is not None and card["artifact_id"] == "art_1" and card["is_starred"] is True

    def test_non_200_and_bad_id_return_none(self, monkeypatch):
        import httpx

        from fusion_cowork.server import web_api

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        assert asyncio.run(web_api._fetch_artifact_card("art_1", transport=httpx.MockTransport(handler))) is None
        # 非法 id (路径注入面) 不发请求
        assert (
            asyncio.run(web_api._fetch_artifact_card("../etc/passwd", transport=httpx.MockTransport(handler))) is None
        )
        assert asyncio.run(web_api._fetch_artifact_card("")) is None

    def test_connection_failure_returns_none(self, monkeypatch):
        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_ARTIFACTS_URL", "http://127.0.0.1:1")  # 不可达端口
        assert asyncio.run(web_api._fetch_artifact_card("art_1")) is None


class TestArtifactRefRoute:
    def test_route_returns_card_with_ref_text(self, client, monkeypatch):
        tc, _ = client
        from fusion_cowork.server import web_api

        async def fake_fetch(artifact_id, transport=None):
            assert artifact_id == "art_1"
            return {
                "artifact_id": "art_1",
                "name": "Q3 Report",
                "type": "report",
                "kind": "document",
                "current_version": 3,
                "summary": "十月对账异常项汇总",
                "source_module": "fusion-doc",
                "is_starred": True,
                "deep_link": "/shell/fusion-artifacts/#/a/art_1",
            }, 200

        monkeypatch.setattr(web_api, "_fetch_artifact_card_with_status", fake_fetch)
        r = tc.get("/api/artifacts/refs/art_1", headers=_headers())
        assert r.status_code == 200, r.text
        card = r.json()["artifact"]
        assert card["deep_link"] == "/shell/fusion-artifacts/#/a/art_1"
        # ref_text 是引擎 ~30 token 轻量格式, 可被 find_artifact_refs 再提取
        assert "[Artifact: Q3 Report | ID: art_1" in card["ref_text"]

    def test_route_upstream_unavailable_502(self, client, monkeypatch):
        tc, _ = client
        from fusion_cowork.server import web_api

        async def fake_fetch(artifact_id, transport=None):
            return None, None

        monkeypatch.setattr(web_api, "_fetch_artifact_card_with_status", fake_fetch)
        r = tc.get("/api/artifacts/refs/art_1", headers=_headers())
        assert r.status_code == 502
        assert r.json()["detail"]["code"] == "upstream_unavailable"

    def test_route_bad_id_404(self, client):
        tc, _ = client
        r = tc.get("/api/artifacts/refs/bad%20id", headers=_headers())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "bad_id"
