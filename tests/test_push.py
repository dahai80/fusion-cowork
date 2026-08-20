from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fusion_cowork.engine.node import NodeConfig, NodeRegistry
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.notification.push import (
    LOCAL_FALLBACK,
    PushConfig,
    push,
)

import_all_nodes()


def _mock_resp(status_code=200, text="ok", url="http://x"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.url = url
    return resp


def test_resolved_provider_auto():
    assert PushConfig(provider="auto", url="https://api.day.app/abc").resolved_provider() == "bark"
    assert PushConfig(provider="auto", url="https://ntfy.sh").resolved_provider() == "ntfy"
    assert PushConfig(provider="auto", url="").resolved_provider() == LOCAL_FALLBACK
    assert PushConfig(provider="bark", url="").resolved_provider() == "bark"


def test_push_empty_message():
    result = asyncio.run(push("t", ""))
    assert result.success is False
    assert "不能为空" in result.error


def test_push_bark_success():
    with patch("fusion_cowork.notification.push.httpx.AsyncClient") as mock_client_cls:
        client = MagicMock()
        client.post = AsyncMock(return_value=_mock_resp(200, "success", "http://bark/abc"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = client
        result = asyncio.run(
            push("标题", "内容", provider="bark", url="https://api.day.app", token="abc", sound="minuet")
        )
        assert result.success is True
        assert result.provider == "bark"
        client.post.assert_awaited_once()
        args, kwargs = client.post.call_args
        assert "abc" in args[0]
        assert kwargs["json"]["title"] == "标题"
        assert kwargs["json"]["sound"] == "minuet"


def test_push_ntfy_success():
    with patch("fusion_cowork.notification.push.httpx.AsyncClient") as mock_client_cls:
        client = MagicMock()
        client.post = AsyncMock(return_value=_mock_resp(200, "ok", "http://ntfy/topic"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = client
        result = asyncio.run(push("T", "M", provider="ntfy", url="https://ntfy.sh", token="mytopic", priority="3"))
        assert result.success is True
        assert result.provider == "ntfy"
        args, kwargs = client.post.call_args
        assert "mytopic" in args[0]
        assert kwargs["content"] == "M"
        assert kwargs["headers"]["Title"] == "T"
        assert kwargs["headers"]["Priority"] == "3"


def test_push_http_error_degrades_local():
    with patch("fusion_cowork.notification.push.httpx.AsyncClient") as mock_client_cls:
        client = MagicMock()
        client.post = AsyncMock(side_effect=ConnectionError("boom"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = client
        result = asyncio.run(push("T", "M", provider="ntfy", url="https://ntfy.sh", token="topic"))
        assert result.provider == LOCAL_FALLBACK
        assert result.degraded is True
        assert "boom" in result.error or result.success


def test_push_node_registered():
    node = NodeRegistry.get("push")
    assert node is not None
    assert node.name == "push"


def test_push_node_empty_message():
    node = NodeRegistry.create("push", config=NodeConfig(params={"provider": "local"}))
    r = asyncio.run(node.execute({"title": "t", "message": ""}))
    assert r.status.value == "failed"
    assert "不能为空" in r.error
