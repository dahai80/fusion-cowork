"""#118 W17: 全局模型选择 — Space agent 聊天接受 model 参数。"""

from __future__ import annotations

import asyncio

from fusion_cowork.space.chat import SpaceChatService
from fusion_cowork.space.models import SpaceMessage


class _Store:
    async def add_message(self, msg):
        if not msg.id:
            msg.id = "msg-118"
        return msg

    async def get_messages(self, space_id, limit=100):
        return []

    async def get_agent_def(self, space_id, agent_id):
        return {"id": agent_id, "system_prompt": "", "config": {"model": "config-model"}}


class _Mlx:
    def __init__(self):
        self.models_used: list[str] = []

    async def list_models(self):
        return [{"id": "fallback-model"}]

    async def stream_chat(self, model, messages):
        self.models_used.append(model)
        yield "ok"


class _Perm:
    async def check(self, space_id, user_id, action):
        return True


def _service():
    mlx = _Mlx()
    return SpaceChatService(_Store(), mlx, _Perm()), mlx


class TestSpaceAgentModel:
    async def test_explicit_model_wins_over_agent_config(self):
        service, mlx = _service()
        await service._agent_respond(
            "space-118",
            "agent-1",
            SpaceMessage(space_id="space-118", user_id="u", content="hi"),
            model="picked-model",
        )
        assert mlx.models_used == ["picked-model"]

    async def test_agent_config_model_used_when_unset(self):
        service, mlx = _service()
        await service._agent_respond(
            "space-118",
            "agent-1",
            SpaceMessage(space_id="space-118", user_id="u", content="hi"),
        )
        assert mlx.models_used == ["config-model"]

    async def test_send_message_forwards_model(self):
        service, _ = _service()
        captured = {}

        async def _capture(space_id, agent_id, msg, model=""):
            captured["model"] = model

        service._agent_respond = _capture
        await service.send_message(
            "space-118",
            "u",
            "hi",
            agent_id="agent-1",
            model="picked-model",
        )
        await asyncio.sleep(0)
        assert captured["model"] == "picked-model"
