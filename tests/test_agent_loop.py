from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from fusion_cowork.agent_loop.loop import (
    AgentLoop,
    _parse_action,
    run_agent_loop,
)
from fusion_cowork.engine.node import BaseNode, NodeRegistry, NodeResult, NodeStatus, register_node


def _llm_resp(content):
    r = MagicMock()
    r.content = content
    return r


@register_node
class _MockEchoNode(BaseNode):
    name = "_mock_echo"
    display_name = "Mock Echo"
    description = "回显参数, 测试用"

    async def execute(self, inputs):
        msg = self.config.params.get("text", "")
        return NodeResult(status=NodeStatus.SUCCESS, data={"echo": msg}, summary=f"echo={msg}")


def test_parse_action_run_node():
    raw = '{"type":"RUN_NODE","node":"桌面清理","params":{"x":"1"},"message":"清理桌面"}'
    a = _parse_action(raw)
    assert a.type == "RUN_NODE"
    assert a.node == "桌面清理"
    assert a.params == {"x": "1"}
    assert a.message == "清理桌面"


def test_parse_action_markdown_wrapped():
    raw = '```json\n{"type":"DONE","message":"完成"}\n```'
    a = _parse_action(raw)
    assert a.type == "DONE"
    assert a.message == "完成"


def test_parse_action_invalid_falls_back_reply():
    a = _parse_action("这完全不是 JSON")
    assert a.type == "REPLY"


def test_parse_action_unknown_type_defaults_reply():
    a = _parse_action('{"type":"FLY","message":"飞"}')
    assert a.type == "REPLY"
    assert a.message == "飞"


def test_agent_loop_done_immediately():
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[_llm_resp('{"type":"DONE","message":"已完成"}')])
    client.list_models = AsyncMock(return_value=[{"id": "m"}])
    loop = AgentLoop(mlx_client=client, model="m", max_steps=5)
    result = asyncio.run(loop.run("你好"))
    assert result.completed is True
    assert result.degraded is False
    assert len(result.turns) >= 2
    assert any(t.action and t.action.type == "DONE" for t in result.turns)


def test_agent_loop_run_node_then_done():
    NodeRegistry.register(_MockEchoNode)
    client = MagicMock()
    client.chat = AsyncMock(
        side_effect=[
            _llm_resp('{"type":"RUN_NODE","node":"_mock_echo","params":{"text":"hi"},"message":"执行回显"}'),
            _llm_resp('{"type":"DONE","message":"结束"}'),
        ]
    )
    client.list_models = AsyncMock(return_value=[{"id": "m"}])
    loop = AgentLoop(mlx_client=client, model="m", max_steps=5)
    result = asyncio.run(loop.run("回显 hi"))
    assert result.completed is True
    run_turns = [t for t in result.turns if t.action and t.action.type == "RUN_NODE"]
    assert len(run_turns) == 1
    assert '"echo": "hi"' in run_turns[0].observation


def test_agent_loop_llm_fail_degrades():
    client = MagicMock()
    client.chat = AsyncMock(side_effect=ConnectionError("no llm"))
    client.list_models = AsyncMock(return_value=[])
    loop = AgentLoop(mlx_client=client, model="", max_steps=5)
    result = asyncio.run(loop.run("做点什么"))
    assert result.degraded is True
    assert result.completed is False


def test_agent_loop_ask_stops():
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[_llm_resp('{"type":"ASK","message":"你要清理哪个目录?"}')])
    client.list_models = AsyncMock(return_value=[{"id": "m"}])
    loop = AgentLoop(mlx_client=client, model="m", max_steps=5)
    result = asyncio.run(loop.run("清理目录"))
    assert result.completed is False
    assert any(t.action and t.action.type == "ASK" for t in result.turns)


def test_agent_loop_interrupt():
    client = MagicMock()
    client.chat = AsyncMock(
        side_effect=[
            _llm_resp('{"type":"RUN_NODE","node":"_mock_echo","params":{"text":"a"},"message":"a"}'),
            _llm_resp('{"type":"RUN_NODE","node":"_mock_echo","params":{"text":"b"},"message":"b"}'),
        ]
    )
    client.list_models = AsyncMock(return_value=[{"id": "m"}])
    NodeRegistry.register(_MockEchoNode)
    loop = AgentLoop(mlx_client=client, model="m", max_steps=10)

    def stop_after_first(turn):
        if turn.action and turn.action.type == "RUN_NODE":
            loop.interrupt()

    loop._on_turn = stop_after_first
    result = asyncio.run(loop.run("跑两步"))
    assert result.interrupted is True


def test_agent_loop_supplement_injected():
    client = MagicMock()
    client.chat = AsyncMock(
        side_effect=[
            _llm_resp('{"type":"RUN_NODE","node":"_mock_echo","params":{"text":"a"},"message":"a"}'),
            _llm_resp('{"type":"DONE","message":"收尾"}'),
        ]
    )
    client.list_models = AsyncMock(return_value=[{"id": "m"}])
    NodeRegistry.register(_MockEchoNode)
    loop = AgentLoop(mlx_client=client, model="m", max_steps=10)

    async def inject_supplement():
        await loop.supplement("再补充一句")

    async def main():
        t = asyncio.create_task(inject_supplement())
        await asyncio.sleep(0.01)
        res = await loop.run("开始")
        await t
        return res

    result = asyncio.run(main())
    assert result.completed is True
    assert any("[补充]" in t.content for t in result.turns)


def test_run_agent_loop_helper():
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[_llm_resp('{"type":"DONE","message":"ok"}')])
    client.list_models = AsyncMock(return_value=[{"id": "m"}])
    result = asyncio.run(run_agent_loop("你好", model="m", mlx_client=client, max_steps=3))
    assert result.completed is True


def test_agent_loop_max_steps_reached():
    client = MagicMock()
    client.chat = AsyncMock(
        side_effect=[
            _llm_resp('{"type":"RUN_NODE","node":"_mock_echo","params":{"text":"x"},"message":"继续"}')
            for _ in range(10)
        ]
    )
    client.list_models = AsyncMock(return_value=[{"id": "m"}])
    NodeRegistry.register(_MockEchoNode)
    loop = AgentLoop(mlx_client=client, model="m", max_steps=3)
    result = asyncio.run(loop.run("无限循环"))
    assert result.completed is False
    assert result.interrupted is False
