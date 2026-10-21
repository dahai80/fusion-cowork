# fusion_cowork.memory_bridge 单测 (issue #157)。
# wire 契约: /v1/memory/{retrieve,commit} + Bearer; fm-core interaction.rs 字段形状。

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from fusion_cowork.memory_bridge import MemoryBridge, _compact


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _clean_env(monkeypatch):
    monkeypatch.delenv("FUSION_MEMORY_API_KEY", raising=False)
    monkeypatch.delenv("FUSION_MEMORY_BASE_URL", raising=False)


def _step(name="搜索节点", status_value="success", summary="找到 3 条", output=None):
    return SimpleNamespace(
        node_id="n1",
        node_name="web_search",
        node_display_name=name,
        status=SimpleNamespace(value=status_value),
        summary=summary,
        error=None if status_value == "success" else "boom",
        output_data=output if output is not None else {"hits": 3},
    )


# ── from_env ──


def test_from_env_no_key_returns_none(monkeypatch):
    _clean_env(monkeypatch)
    assert MemoryBridge.from_env() is None


def test_from_env_with_key(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    bridge = MemoryBridge.from_env()
    assert bridge is not None
    assert bridge._base_url == "http://127.0.0.1:11435"
    assert bridge._api_key == "k1"


def test_from_env_base_url_override(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    monkeypatch.setenv("FUSION_MEMORY_BASE_URL", "http://127.0.0.1:9999/")
    bridge = MemoryBridge.from_env()
    assert bridge._base_url == "http://127.0.0.1:9999"


# ── build_interaction ──


def test_build_interaction_shape():
    steps = [_step("搜索", "success", "找到 3 条"), _step("写入", "failed", "", {"k": "v"})]
    ix = MemoryBridge.build_interaction(
        session_id="run_abc",
        workflow_id="wf1",
        workflow_name="周报生成",
        description="每周五生成",
        steps=steps,
        result_summary="完成",
        execution_id="run_abc",
    )
    assert ix["id"].startswith("cowork_")
    assert ix["session_id"] == "run_abc"
    assert isinstance(ix["timestamp"], int) and abs(ix["timestamp"] - time.time()) < 60
    turn = ix["turns"][0]
    assert turn["turn_idx"] == 0
    assert turn["user_message"] == "周报生成: 每周五生成"
    assert turn["assistant_message"] == "完成"
    assert len(turn["tool_calls"]) == 2
    assert turn["tool_calls"][0] == {
        "name": "搜索",
        "args": {},
        "result_summary": "[success] 找到 3 条",
    }
    # 失败步骤: error 即摘要 (fail-visible), 带 status 前缀
    assert turn["tool_calls"][1]["result_summary"] == "[failed] boom"
    # 无 summary 无 error 的步骤用 _compact(output_data) 兜底
    ix2 = MemoryBridge.build_interaction(
        session_id="s",
        workflow_id="",
        workflow_name="wf",
        description="",
        steps=[_step("节点", "success", "", {"k": "v"})],
        result_summary="ok",
    )
    assert '"k": "v"' in ix2["turns"][0]["tool_calls"][0]["result_summary"]
    assert ix["metadata"]["agent_type"] == "cowork"
    assert ix["metadata"]["node_id"] == "wf1"
    assert ix["metadata"]["tool_names"] == ["搜索", "写入"]


def test_build_interaction_caps_and_defaults():
    long_summary = "x" * 5000
    steps = [_step(summary=long_summary) for _ in range(70)]
    ix = MemoryBridge.build_interaction(
        session_id="s", workflow_id="", workflow_name="wf", description="", steps=steps, result_summary=""
    )
    assert len(ix["turns"][0]["tool_calls"]) == 64
    assert all(len(tc["result_summary"]) <= 4000 for tc in ix["turns"][0]["tool_calls"])
    # 空 summary/结果 → "(无输出)"
    assert ix["turns"][0]["assistant_message"] == "(无输出)"
    # 空 workflow_id 回落 execution_id... 此处都空 → node_id 空串不崩
    assert "node_id" in ix["metadata"]


# ── retrieve_context ──


@pytest.mark.asyncio
async def test_retrieve_ok(monkeypatch):
    _clean_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memory/retrieve"
        assert request.headers.get("authorization") == "Bearer k1"
        payload = json.loads(request.content)
        assert payload["method"] == "retrieve"
        assert payload["params"]["text"] == "周报"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {
                    "blocks": [
                        {"turns_text": "上周产出: 报表 A"},
                        {"turns_text": "偏好: 表格输出"},
                    ],
                    "total_tokens": 42,
                },
                "id": 1,
            },
        )

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    bridge = MemoryBridge.from_env()
    text = await bridge.retrieve_context("周报")
    assert text == "上周产出: 报表 A\n\n偏好: 表格输出"


@pytest.mark.asyncio
async def test_retrieve_rpc_error_returns_none(monkeypatch):
    _clean_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "error": {"code": -32003, "message": "busy"}, "id": 1},
        )

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    bridge = MemoryBridge.from_env()
    assert await bridge.retrieve_context("q") is None


@pytest.mark.asyncio
async def test_retrieve_http_500_returns_none(monkeypatch):
    _clean_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    bridge = MemoryBridge.from_env()
    assert await bridge.retrieve_context("q") is None


@pytest.mark.asyncio
async def test_retrieve_empty_query_skips_http(monkeypatch):
    _clean_env(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"blocks": []}, "id": 1})

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    bridge = MemoryBridge.from_env()
    assert await bridge.retrieve_context("   ") is None
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_retrieve_empty_blocks_returns_none(monkeypatch):
    _clean_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"blocks": [], "total_tokens": 0}, "id": 1})

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    bridge = MemoryBridge.from_env()
    assert await bridge.retrieve_context("q") is None


# ── write_back ──


@pytest.mark.asyncio
async def test_write_back_dict_contract(monkeypatch):
    _clean_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memory/commit"
        assert request.headers.get("authorization") == "Bearer k1"
        payload = json.loads(request.content)
        seen.update(payload["params"])
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {"memory_ids": ["m1", "m2"], "failed_turns": []},
                "id": 1,
            },
        )

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    bridge = MemoryBridge.from_env()
    ix = MemoryBridge.build_interaction(
        session_id="run_x",
        workflow_id="wf",
        workflow_name="任务",
        description="",
        steps=[],
        result_summary="done",
    )
    ids = await bridge.write_back(ix)
    assert ids == ["m1", "m2"]
    assert seen["session_id"] == "run_x"
    assert seen["interaction"]["timestamp"] == ix["timestamp"]


@pytest.mark.asyncio
async def test_write_back_old_list_contract(monkeypatch):
    _clean_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": ["m1"], "id": 1})

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    bridge = MemoryBridge.from_env()
    ids = await bridge.write_back({"session_id": "s"})
    assert ids == ["m1"]


@pytest.mark.asyncio
async def test_write_back_http_error_returns_none(monkeypatch):
    _clean_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    _patch_transport(monkeypatch, handler)
    monkeypatch.setenv("FUSION_MEMORY_API_KEY", "k1")
    bridge = MemoryBridge.from_env()
    assert await bridge.write_back({"session_id": "s"}) is None


# ── _compact ──


def test_compact_dict_and_scalar():
    assert _compact({"a": 1}) == '{"a": 1}'
    assert _compact("plain") == "plain"
    assert len(_compact("y" * 9999)) == 4000
    assert _compact(object())  # 任意对象不崩
