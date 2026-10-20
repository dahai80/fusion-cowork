"""#126: /api/models chat-only default.

Contract:
- default: non-chat engine_type filtered (video/embedding/image_gen/stt/tts/rerank)
- ?all=1 returns raw list
- entry without engine_type counts as chat (conservative pass)
- _is_chat_model shared by /api/models and _pick_chat_model
"""

from __future__ import annotations

import pytest

STATIC_KEY = "fc-web-test-126"


class FakeMlx:
    def __init__(self, models=None):
        self.models = (
            models
            if models is not None
            else [
                {"id": "qwen-chat-8b", "engine_type": "chat"},
                {"id": "wan-video", "engine_type": "video_gen"},
                {"id": "embed-bge", "engine_type": "embedding"},
                {"id": "no-engine-entry"},
            ]
        )

    async def list_models(self):
        return self.models


class FakeGenerator:
    def __init__(self, mlx):
        self.mlx = mlx


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        nl_generator=FakeGenerator(FakeMlx()),
    )
    yield TestClient(app), app


class TestModelsChatFilter:
    def test_filters_non_chat_by_engine_type(self, client):
        tc, _ = client
        resp = tc.get("/api/models", headers={"Authorization": f"Bearer {STATIC_KEY}"})
        assert resp.status_code == 200
        body = resp.json()
        assert {m["id"] for m in body["models"]} == {"qwen-chat-8b", "no-engine-entry"}
        assert body["count"] == 2
        assert body["total"] == 4

    def test_all_flag_returns_raw(self, client):
        tc, _ = client
        resp = tc.get(
            "/api/models",
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
            params={"all": "1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert {m["id"] for m in body["models"]} == {"qwen-chat-8b", "wan-video", "embed-bge", "no-engine-entry"}
        assert body["count"] == 4
        assert body["total"] == 4

    def test_missing_engine_type_counts_as_chat(self, client):
        tc, _ = client
        resp = tc.get("/api/models", headers={"Authorization": f"Bearer {STATIC_KEY}"})
        assert resp.status_code == 200
        ids = {m["id"] for m in resp.json()["models"]}
        assert "no-engine-entry" in ids
        assert "wan-video" not in ids


class TestIsChatModel:
    def test_engine_type_gate(self):
        from fusion_cowork.ai.nl_parser import _is_chat_model

        assert _is_chat_model({"id": "a", "engine_type": "chat"}) is True
        assert _is_chat_model({"id": "b", "engine_type": "video_gen"}) is False
        assert _is_chat_model({"id": "c"}) is True
        assert _is_chat_model({"id": "d", "metadata": {"engine_type": "embedding"}}) is False
        assert _is_chat_model("not-a-dict") is False
        assert _is_chat_model({"engine_type": "chat"}) is False

    def test_pick_chat_model_unchanged(self):
        from fusion_cowork.ai.nl_parser import _pick_chat_model

        models = [
            {"id": "wan-video", "engine_type": "video_gen"},
            {"id": "qwen-instruct", "engine_type": "chat"},
        ]
        assert _pick_chat_model(models) == "qwen-instruct"
