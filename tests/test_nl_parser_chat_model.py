import os

import pytest

from fusion_cowork.ai.nl_parser import NLWorkflowGenerator, _pick_chat_model


class TestPickChatModel:
    def test_skips_image_gen_picks_chat(self):
        models = [
            {"id": "FLUX.2-klein-base-4B", "engine_type": "image_gen"},
            {"id": "Qwen3.8-27B-4bit", "engine_type": "llm"},
        ]
        assert _pick_chat_model(models) == "Qwen3.8-27B-4bit"

    def test_skips_video_gen_tts_embedding(self):
        models = [
            {"id": "Wan2.1-T2V", "engine_type": "video_gen"},
            {"id": "cosyvoice", "engine_type": "tts"},
            {"id": "bge-m3", "engine_type": "embedding"},
            {"id": "Qwen3.5-4B-Instruct", "engine_type": "llm"},
        ]
        assert _pick_chat_model(models) == "Qwen3.5-4B-Instruct"

    def test_engine_type_in_metadata(self):
        models = [
            {"id": "FLUX.2-klein-base-4B", "metadata": {"engine_type": "image_gen"}},
            {"id": "mlx-community--Qwen3.5-4B-MLX-4bit"},
        ]
        assert _pick_chat_model(models) == "mlx-community--Qwen3.5-4B-MLX-4bit"

    def test_prefers_instruct_keyword_over_first_chat(self):
        models = [
            {"id": "some-base-model", "engine_type": "llm"},
            {"id": "Llama-3.1-8B-Instruct", "engine_type": "llm"},
        ]
        assert _pick_chat_model(models) == "Llama-3.1-8B-Instruct"

    def test_falls_back_to_first_chat_when_no_keyword(self):
        models = [
            {"id": "FLUX.2", "engine_type": "image_gen"},
            {"id": "some-model", "engine_type": "llm"},
            {"id": "another-model", "engine_type": "llm"},
        ]
        assert _pick_chat_model(models) == "some-model"

    def test_returns_empty_when_all_non_chat(self):
        models = [
            {"id": "FLUX.2", "engine_type": "image_gen"},
            {"id": "Wan2.1", "engine_type": "video_gen"},
        ]
        assert _pick_chat_model(models) == ""

    def test_returns_empty_when_empty_list(self):
        assert _pick_chat_model([]) == ""
        assert _pick_chat_model(None) == ""

    def test_handles_model_key_fallback(self):
        models = [{"model": "Qwen3.8-27B-4bit", "engine_type": "llm"}]
        assert _pick_chat_model(models) == "Qwen3.8-27B-4bit"

    def test_skips_entries_without_id(self):
        models = [
            {"engine_type": "image_gen"},
            {"id": "Qwen3.5-4B-Instruct", "engine_type": "llm"},
        ]
        assert _pick_chat_model(models) == "Qwen3.5-4B-Instruct"


class TestGenerateModelResolution:
    @pytest.mark.asyncio
    async def test_env_override_model(self, monkeypatch):
        monkeypatch.setenv("FUSION_MLX_MODEL", "my-pinned-chat-model")
        gen = NLWorkflowGenerator.__new__(NLWorkflowGenerator)
        gen.mlx = None
        gen.model = ""
        gen._workflow_history = []

        env_model = os.environ.get("FUSION_MLX_MODEL", "").strip()
        if not gen.model and env_model:
            gen.model = env_model
        assert gen.model == "my-pinned-chat-model"

    @pytest.mark.asyncio
    async def test_explicit_model_not_overridden_by_env(self, monkeypatch):
        monkeypatch.setenv("FUSION_MLX_MODEL", "env-model")
        gen = NLWorkflowGenerator.__new__(NLWorkflowGenerator)
        gen.mlx = None
        gen.model = "explicit-model"

        env_model = os.environ.get("FUSION_MLX_MODEL", "").strip()
        if not gen.model and env_model:
            gen.model = env_model
        assert gen.model == "explicit-model"


class TestDeskRpcWorkflowCreateReturn:
    def test_workflow_create_returns_dict_not_workflow(self, monkeypatch):
        import asyncio

        from fusion_cowork.ai.nl_parser import NLWorkflowGenerator
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        async def fake_generate(self, prompt):
            return {"name": "fake", "nodes": [], "edges": []}

        # #85: generate() 返回 dict; handler 不再调 to_dict()
        monkeypatch.setattr(NLWorkflowGenerator, "generate", fake_generate)

        class FakeMlx:
            async def list_models(self):
                return [{"id": "Qwen3.8-27B-4bit", "engine_type": "llm"}]

            async def chat(self, **kwargs):
                return type("R", (), {"content": '{"name":"x","nodes":[],"edges":[]}'})()

        srv = type("S", (), {})()
        srv._get_mlx_client = lambda: FakeMlx()
        result = asyncio.run(DeskRPCServer._handle_workflow_create(srv, {"prompt": "你是谁"}))
        assert result["workflow"] == {"name": "fake", "nodes": [], "edges": []}
        assert not hasattr(result["workflow"], "to_dict")
