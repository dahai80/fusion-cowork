"""P2-9 测试 — MCP Streamable HTTP 传输 (2025-03-26 spec)。

单一 /mcp 端点 POST/DELETE/GET:
- initialize 建 Mcp-Session-Id
- notifications/initialized 标记会话就绪
- tools/list, tools/call
- DELETE 终止会话
- tools/call + Accept: text/event-stream → SSE 流
"""

import json

import pytest

from fusion_cowork.server.mcp_http import (
    STREAMABLE_PROTOCOL_VERSION,
    create_streamable_app,
)
from fusion_cowork.server.mcp_server import MCPToolRegistry


@pytest.fixture
def streamable_client():
    from starlette.testclient import TestClient

    registry = MCPToolRegistry()
    registry.register_tools()
    app = create_streamable_app(registry)
    with TestClient(app) as client:
        yield client


def _init(client):
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t"}}},
        headers={"accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["protocolVersion"] == STREAMABLE_PROTOCOL_VERSION
    sid = resp.headers.get("mcp-session-id")
    assert sid and sid.startswith("mcp-")
    return sid


class TestStreamableInitialize:
    def test_initialize_creates_session(self, streamable_client):
        sid = _init(streamable_client)
        assert sid.startswith("mcp-")

    def test_request_without_session_rejected(self, streamable_client):
        resp = streamable_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={"accept": "application/json"},
        )
        assert resp.status_code == 400
        assert "session" in resp.json()["error"]["message"].lower()

    def test_initialized_notification_returns_202(self, streamable_client):
        sid = _init(streamable_client)
        resp = streamable_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={"mcp-session-id": sid, "accept": "application/json"},
        )
        assert resp.status_code == 202

    def test_method_not_found(self, streamable_client):
        sid = _init(streamable_client)
        resp = streamable_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "nope", "params": {}},
            headers={"mcp-session-id": sid, "accept": "application/json"},
        )
        assert resp.json()["error"]["code"] == -32601


class TestStreamableToolsBeforeInit:
    def test_tools_list_before_initialized_rejected(self, streamable_client):
        sid = _init(streamable_client)
        resp = streamable_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
            headers={"mcp-session-id": sid, "accept": "application/json"},
        )
        assert resp.json()["error"]["code"] == -32002


class TestStreamableTools:
    def test_tools_list_after_initialized(self, streamable_client):
        sid = _init(streamable_client)
        streamable_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={"mcp-session-id": sid, "accept": "application/json"},
        )
        resp = streamable_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
            headers={"mcp-session-id": sid, "accept": "application/json"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "tools" in body["result"]
        assert resp.headers.get("mcp-session-id") == sid

    def test_tools_call_unknown_returns_error_content(self, streamable_client):
        sid = _init(streamable_client)
        streamable_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={"mcp-session-id": sid, "accept": "application/json"},
        )
        resp = streamable_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "/nonexistent/path/xyz"}},
            },
            headers={"mcp-session-id": sid, "accept": "application/json"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "content" in body["result"]


class TestStreamableSSEStream:
    def test_tools_call_with_event_stream_returns_sse(self, streamable_client):
        sid = _init(streamable_client)
        streamable_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={"mcp-session-id": sid, "accept": "application/json"},
        )
        resp = streamable_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "/nonexistent/xyz"}},
            },
            headers={"mcp-session-id": sid, "accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        text = resp.text
        assert "event: message" in text
        payload_line = [ln for ln in text.splitlines() if ln.startswith("data: ")]
        assert payload_line
        payload = json.loads(payload_line[0][len("data: ") :])
        assert payload["id"] == 7


class TestStreamableDelete:
    def test_delete_terminates_session(self, streamable_client):
        sid = _init(streamable_client)
        resp = streamable_client.delete("/mcp", headers={"mcp-session-id": sid})
        assert resp.status_code == 200
        assert resp.json()["status"] == "terminated"

    def test_delete_unknown_session_404(self, streamable_client):
        resp = streamable_client.delete("/mcp", headers={"mcp-session-id": "mcp-nope"})
        assert resp.status_code == 404

    def test_request_after_delete_rejected(self, streamable_client):
        sid = _init(streamable_client)
        streamable_client.delete("/mcp", headers={"mcp-session-id": sid})
        resp = streamable_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
            headers={"mcp-session-id": sid, "accept": "application/json"},
        )
        assert resp.status_code == 400


class TestStreamableHealth:
    def test_health_reports_streamable_protocol(self, streamable_client):
        resp = streamable_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["protocol"] == "streamable-2025-03-26"
        assert body["status"] == "ok"
