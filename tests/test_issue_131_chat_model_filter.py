"""#131: chat-model filter — draft engine_type + gateway capabilities metadata.

Contract (fusion-gateway#245 alignment):
- engine_type "draft" (speculative-decoding draft models) treated as non-chat
- capabilities.embedding=true -> non-chat even when engine_type missing/modality=text
- capabilities.text_generation=false -> non-chat; true -> chat (authoritative second signal)
- capabilities present but without embedding/text_generation keys falls through to engine_type
- metadata.capabilities honored same as top-level
"""

from __future__ import annotations

STATIC_KEY = "fc-web-test-131"


class TestDraftEngineType:
    def test_draft_filtered(self):
        from fusion_cowork.ai.nl_parser import _is_chat_model

        assert _is_chat_model({"id": "Qwen3.8-27B-DFlash2", "engine_type": "draft"}) is False
        assert _is_chat_model({"id": "d", "metadata": {"engine_type": "draft"}}) is False

    def test_draft_not_picked_as_default(self):
        from fusion_cowork.ai.nl_parser import _pick_chat_model

        models = [
            {"id": "Qwen3.8-27B-DFlash2", "engine_type": "draft"},
            {"id": "some-chat-model", "engine_type": "chat"},
        ]
        assert _pick_chat_model(models) == "some-chat-model"


class TestCapabilitiesSignal:
    def test_embedding_capability_wins(self):
        from fusion_cowork.ai.nl_parser import _is_chat_model

        # bge-m3: modality=text (无 engine_type 时会误判 chat), capabilities 纠正
        m = {"id": "bge-m3", "modality": "text", "capabilities": {"embedding": True}}
        assert _is_chat_model(m) is False

    def test_text_generation_false_non_chat(self):
        from fusion_cowork.ai.nl_parser import _is_chat_model

        m = {"id": "x", "capabilities": {"text_generation": False}}
        assert _is_chat_model(m) is False

    def test_text_generation_true_chat(self):
        from fusion_cowork.ai.nl_parser import _is_chat_model

        m = {"id": "x", "engine_type": "chat", "capabilities": {"text_generation": True}}
        assert _is_chat_model(m) is True

    def test_unrelated_capabilities_fall_through(self):
        from fusion_cowork.ai.nl_parser import _is_chat_model

        m = {"id": "x", "capabilities": {"vision": True}}
        assert _is_chat_model(m) is True
        assert _is_chat_model({"id": "y", "capabilities": {"vision": True}, "engine_type": "tts"}) is False

    def test_metadata_capabilities_honored(self):
        from fusion_cowork.ai.nl_parser import _is_chat_model

        m = {"id": "bge-m3", "metadata": {"capabilities": {"embedding": True}}}
        assert _is_chat_model(m) is False

    def test_embedding_false_falls_through(self):
        from fusion_cowork.ai.nl_parser import _is_chat_model

        m = {"id": "x", "capabilities": {"embedding": False}}
        assert _is_chat_model(m) is True


class TestApiModelsGateway:
    def test_gateway_models_filtered(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        web_api._WEB_DIR = tmp_path / "web"

        models = [
            {"id": "Qwen3.8-27B-DFlash2", "modality": "draft", "engine_type": "draft"},
            {
                "id": "bge-m3",
                "modality": "text",
                "capabilities": {"embedding": True},
            },
            {"id": "qwen3-instruct", "engine_type": "chat", "capabilities": {"text_generation": True}},
        ]

        class FakeMlx:
            async def list_models(self):
                return models

        class FakeGenerator:
            def __init__(self, mlx):
                self.mlx = mlx

        app = web_api.create_web_app(
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "workflows",
            nl_generator=FakeGenerator(FakeMlx()),
        )
        tc = TestClient(app)
        resp = tc.get("/api/models", headers={"Authorization": f"Bearer {STATIC_KEY}"})
        assert resp.status_code == 200
        body = resp.json()
        assert [m["id"] for m in body["models"]] == ["qwen3-instruct"]
        assert body["count"] == 1
        assert body["total"] == 3
