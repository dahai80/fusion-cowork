from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import fusion_cowork.ai.mlx_client as mc
from fusion_cowork.ai.mlx_client import FusionMLXClient

# 模块级快照真实环境 flag (CI 无 fusion-core=False, 本地有=True). 每测试 finally 还原此值, 杜绝全局污染.
_ORIG_HAS_FUSION_CORE = mc._HAS_FUSION_CORE


def _fake_resp(content: str, tool_calls: list | None = None, finish_reason: str = "stop"):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [
            {"message": {"content": content, "tool_calls": tool_calls or []}, "finish_reason": finish_reason}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    return resp


@pytest.mark.asyncio
async def test_fallback_path_normal_chat():
    mc._HAS_FUSION_CORE = False
    try:
        client = FusionMLXClient(base_url="http://localhost:11432/v1", api_key="k")
        mock_c = MagicMock()
        mock_c.post = AsyncMock(return_value=_fake_resp("hello"))
        client._client = mock_c
        r = await client.chat("m", [{"role": "user", "content": "x"}])
        assert r.content == "hello"
        assert r.finish_reason == "stop"
        assert mock_c.post.await_count == 1
    finally:
        mc._HAS_FUSION_CORE = _ORIG_HAS_FUSION_CORE


@pytest.mark.asyncio
async def test_fallback_path_dh3_empty_content_guard():
    mc._HAS_FUSION_CORE = False
    try:
        client = FusionMLXClient(base_url="http://localhost:11432/v1", api_key="k")
        mock_c = MagicMock()
        mock_c.post = AsyncMock(return_value=_fake_resp(""))
        client._client = mock_c
        r = await client.chat("m", [{"role": "user", "content": "x"}])
        assert r.content == ""
        assert r.finish_reason == "empty_content"
    finally:
        mc._HAS_FUSION_CORE = _ORIG_HAS_FUSION_CORE


@pytest.mark.asyncio
async def test_dh3_guard_skipped_when_tool_calls_present():
    mc._HAS_FUSION_CORE = False
    try:
        client = FusionMLXClient(base_url="http://localhost:11432/v1", api_key="k")
        mock_c = MagicMock()
        mock_c.post = AsyncMock(
            return_value=_fake_resp("", tool_calls=[{"id": "1"}], finish_reason="tool_calls")
        )
        client._client = mock_c
        r = await client.chat("m", [{"role": "user", "content": "x"}])
        assert r.finish_reason == "tool_calls"
        assert r.tool_calls == [{"id": "1"}]
    finally:
        mc._HAS_FUSION_CORE = _ORIG_HAS_FUSION_CORE


@pytest.mark.asyncio
async def test_core_path_uses_with_retry():
    mc._HAS_FUSION_CORE = True
    try:
        mock_c = MagicMock()
        fake_resp = _fake_resp("core-hello")
        with patch.object(mc, "_get_async_client", return_value=mock_c), \
             patch.object(mc, "_with_retry", new=AsyncMock(return_value=fake_resp)) as wr:
            client = FusionMLXClient(base_url="http://localhost:11432/v1", api_key="k")
            r = await client.chat("m", [{"role": "user", "content": "x"}])
            assert r.content == "core-hello"
            assert wr.await_count == 1
    finally:
        mc._HAS_FUSION_CORE = _ORIG_HAS_FUSION_CORE


@pytest.mark.asyncio
async def test_core_path_dh3_empty_content_guard():
    mc._HAS_FUSION_CORE = True
    try:
        mock_c = MagicMock()
        fake_resp = _fake_resp("")
        with patch.object(mc, "_get_async_client", return_value=mock_c), \
             patch.object(mc, "_with_retry", new=AsyncMock(return_value=fake_resp)):
            client = FusionMLXClient(base_url="http://localhost:11432/v1", api_key="k")
            r = await client.chat("m", [{"role": "user", "content": "x"}])
            assert r.content == ""
            assert r.finish_reason == "empty_content"
    finally:
        mc._HAS_FUSION_CORE = _ORIG_HAS_FUSION_CORE


@pytest.mark.asyncio
async def test_import_guard_flag_resolved():
    # flag 须 bool, 且与 _with_retry/_get_async_client 绑定一致 (True→非None, False→None).
    # CI 无 fusion-core 时=False, 本地有=True, 双环境都该过.
    assert isinstance(mc._HAS_FUSION_CORE, bool)
    if mc._HAS_FUSION_CORE:
        assert mc._with_retry is not None
        assert mc._get_async_client is not None
    else:
        assert mc._with_retry is None
        assert mc._get_async_client is None
