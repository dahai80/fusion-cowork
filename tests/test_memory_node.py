"""MemoryCommitNode / MemoryRetrieveNode 单元测试 — fusion-memory HTTP 节点。

测注册 / 别名 / 参数校验 / fail-visible 鉴权缺失 / HTTP wire 契约 (httpx
MockTransport 离线桩, 不连真 fusion-memory, 符合离线测试约束)。
"""

from __future__ import annotations

import json

import httpx
import pytest

import fusion_cowork.nodes.ecosystem  # noqa: F401  触发 @register_node
from fusion_cowork import NODE_NAME_ALIASES
from fusion_cowork.engine.node import NodeCategory, NodeConfig, NodeRegistry, NodeStatus
from fusion_cowork.nodes.ecosystem.memory_node import (
    MemoryCommitNode,
    MemoryRetrieveNode,
)


def test_commit_node_registered():
    node = NodeRegistry.get("memory_commit")
    assert node is MemoryCommitNode
    assert node.category == NodeCategory.FUSION_ECOSYSTEM
    assert node.name == "memory_commit"


def test_retrieve_node_registered():
    node = NodeRegistry.get("memory_retrieve")
    assert node is MemoryRetrieveNode
    assert node.category == NodeCategory.FUSION_ECOSYSTEM
    assert node.name == "memory_retrieve"


def test_aliases_registered():
    assert NODE_NAME_ALIASES.get("记忆提交") == "memory_commit"
    assert NODE_NAME_ALIASES.get("提交记忆") == "memory_commit"
    assert NODE_NAME_ALIASES.get("记忆检索") == "memory_retrieve"
    assert NODE_NAME_ALIASES.get("召回记忆") == "memory_retrieve"


def test_commit_params_schema_required_fields():
    node = MemoryCommitNode(node_id="c1")
    schema = node.get_params_schema()
    assert "session_id" in schema["required"]
    assert "interaction" in schema["required"]


def test_retrieve_params_schema_required_fields():
    node = MemoryRetrieveNode(node_id="r1")
    schema = node.get_params_schema()
    assert "text" in schema["required"]
    assert schema["properties"]["top_k"]["default"] == 10
    assert schema["properties"]["token_budget"]["default"] == 4096


def _make_commit_node(monkeypatch, handler, api_key="test-key"):
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", api_key)
    transport = httpx.MockTransport(handler)
    # 注入 mock transport: 包装 _rpc 用的 AsyncClient
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return MemoryCommitNode(
        node_id="c1",
        config=NodeConfig(
            params={
                "session_id": "sess-1",
                "interaction": {
                    "id": "ix-1",
                    "session_id": "sess-1",
                    "turns": [
                        {
                            "turn_idx": 0,
                            "user_message": "how to rank vector search",
                            "assistant_message": "cosine similarity",
                            "tool_calls": [],
                        }
                    ],
                    "timestamp": 1,
                    "metadata": {},
                },
            }
        ),
    )


def _make_retrieve_node(monkeypatch, handler, api_key="test-key"):
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", api_key)
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return MemoryRetrieveNode(
        node_id="r1",
        config=NodeConfig(params={"text": "vector search rank", "top_k": 5}),
    )


def _commit_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v1/memory/commit"
    assert request.headers.get("authorization") == "Bearer test-key"
    payload = json.loads(request.content)
    assert payload["method"] == "commit"
    assert payload["params"]["session_id"] == "sess-1"
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "result": ["mem-1", "mem-2"], "id": 1},
    )


@pytest.mark.asyncio
async def test_commit_http_wire_success(monkeypatch):
    node = _make_commit_node(monkeypatch, _commit_handler)
    result = await node.execute({})
    assert result.status == NodeStatus.SUCCESS
    assert result.data["memory_ids"] == ["mem-1", "mem-2"]
    assert result.data["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_commit_missing_session_id_fails(monkeypatch):
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "test-key")
    node = MemoryCommitNode(node_id="c1", config=NodeConfig(params={"interaction": {"id": "x"}}))
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "session_id" in result.error


@pytest.mark.asyncio
async def test_commit_missing_api_key_fails(monkeypatch):
    monkeypatch.delenv("FUSION_MEMORY_API_KEY", raising=False)
    node = MemoryCommitNode(
        node_id="c1",
        config=NodeConfig(params={"session_id": "s", "interaction": {"id": "x"}}),
    )
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "FUSION_MEMORY_API_KEY" in result.error


def _retrieve_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v1/memory/retrieve"
    payload = json.loads(request.content)
    assert payload["method"] == "retrieve"
    assert payload["params"]["text"] == "vector search rank"
    assert payload["params"]["top_k"] == 5
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "result": {
                "blocks": [
                    {
                        "interaction_id": "ix-1",
                        "turns": [],
                        "memory_type": "Episodic",
                        "turns_text": "how to rank vector search",
                        "score": 0.9,
                        "source_entities": ["vector_search"],
                    }
                ],
                "total_tokens": 42,
            },
            "id": 2,
        },
    )


@pytest.mark.asyncio
async def test_retrieve_http_wire_success(monkeypatch):
    node = _make_retrieve_node(monkeypatch, _retrieve_handler)
    result = await node.execute({})
    assert result.status == NodeStatus.SUCCESS
    assert result.data["block_count"] == 1
    assert result.data["total_tokens"] == 42
    assert result.data["context"]["blocks"][0]["interaction_id"] == "ix-1"


@pytest.mark.asyncio
async def test_retrieve_missing_text_fails(monkeypatch):
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "test-key")
    node = MemoryRetrieveNode(node_id="r1", config=NodeConfig(params={}))
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "text" in result.error


@pytest.mark.asyncio
async def test_retrieve_rpc_error_propagates(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "error": {"code": -32602, "message": "bad params"}, "id": 3},
        )

    node = _make_retrieve_node(monkeypatch, handler)
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "-32602" in result.error
