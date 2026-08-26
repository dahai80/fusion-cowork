"""Issue #48 — /rpc 端点 + plugins/* 委托测试。

覆盖:
- rpc_bridge.dispatch_rpc 路由 plugins/* + 拒绝非 plugins 方法
- mcp_http /rpc 路由 (plugins.ping / plugins/list / plugins/config.get)
- 节点发现 (list_nodes_for_plugins 返回 NodeRegistry.list)
- 依赖缺失降级
- JSON 解析错误
"""

import pytest

from fusion_cowork.server.rpc_bridge import (
    PLUGINS_METHODS,
    dispatch_rpc,
    is_plugins_available,
    list_nodes_for_plugins,
)


def test_plugins_methods_whitelist():
    assert "plugins.ping" in PLUGINS_METHODS
    assert "plugins/list" in PLUGINS_METHODS
    assert len(PLUGINS_METHODS) == 15


def test_is_plugins_available():
    assert isinstance(is_plugins_available(), bool)


@pytest.mark.asyncio
async def test_dispatch_rpc_rejects_non_plugins_method():
    resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["error"]["code"] == -32601
    assert "/rpc 仅托管 plugins/*" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_dispatch_rpc_unknown_plugins_method():
    # LO-10: 点格式非白名单方法 (plugins.bogus) → guard 层 -32601, 不进 handler
    # (guard: method in PLUGINS_METHODS or method.startswith("plugins/"))
    resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 2, "method": "plugins.bogus", "params": {}})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 2
    assert resp["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_dispatch_rpc_rejects_slash_unknown_plugins():
    # LO-10: 斜杠前缀宽匹配 (plugins/bogus 命中 startswith("plugins/")) → 委托 MCPHandler
    # 依赖在 → handler 返回 -32601 (unknown method); 依赖缺 → ImportError → -32603
    resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 9, "method": "plugins/bogus", "params": {}})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 9
    if is_plugins_available():
        assert resp["error"]["code"] == -32601
    else:
        assert resp["error"]["code"] == -32603


@pytest.mark.asyncio
@pytest.mark.skipif(not is_plugins_available(), reason="需要 fusion-plugins-ecosystem")
async def test_dispatch_rpc_plugins_ping():
    resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 3, "method": "plugins.ping", "params": {}})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 3
    assert resp["result"] == {"pong": True}


@pytest.mark.asyncio
@pytest.mark.skipif(not is_plugins_available(), reason="需要 fusion-plugins-ecosystem")
async def test_dispatch_rpc_plugins_list():
    resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 4, "method": "plugins/list", "params": {}})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 4
    assert "plugins" in resp["result"]
    assert isinstance(resp["result"]["plugins"], list)
    # issue #52: register_builtin 后内置插件可发现 (非空)
    assert len(resp["result"]["plugins"]) > 0
    ids = {p["id"] for p in resp["result"]["plugins"]}
    assert "caveman_compress" in ids


@pytest.mark.asyncio
@pytest.mark.skipif(not is_plugins_available(), reason="需要 fusion-plugins-ecosystem")
async def test_dispatch_rpc_auto_mount_defaults():
    # issue #52: 首次 dispatch 后 default_mounted 插件应已 enable,
    # state.get 返回 enabled (而非 disabled/unknown)
    resp = await dispatch_rpc(
        {"jsonrpc": "2.0", "id": 6, "method": "plugins/state.get", "params": {"plugin_id": "caveman_compress"}}
    )
    assert resp["jsonrpc"] == "2.0"
    assert resp["result"]["state"] == "enabled"


@pytest.mark.asyncio
@pytest.mark.skipif(not is_plugins_available(), reason="需要 fusion-plugins-ecosystem")
async def test_dispatch_rpc_plugins_config_get():
    resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 5, "method": "plugins/config.get", "params": {}})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 5
    cfg = resp["result"]
    # Studio 7 字段投影
    for key in (
        "sandbox_mode",
        "auto_update",
        "max_concurrent_plugins",
        "log_level",
        "token_budget",
        "vram_limit_mb",
        "mcp_enabled",
    ):
        assert key in cfg


def test_list_nodes_for_plugins_returns_registry():
    # 确保节点模块已注册 (触发 @register_node)
    import fusion_cowork.nodes.io.file_io
    import fusion_cowork.nodes.macos.system_nodes
    import fusion_cowork.nodes.tools.tool_nodes  # noqa: F401

    nodes = list_nodes_for_plugins()
    assert isinstance(nodes, list)
    assert len(nodes) > 0
    names = {n["name"] for n in nodes}
    assert "file_input" in names or "FileInputNode" in names or any("input" in n.lower() for n in names)


# ── HTTP /rpc 路由 ──


class TestRpcHttpRoute:
    @pytest.fixture
    def app(self):
        pytest.importorskip("fastapi", reason="需要 [web] 依赖")
        from fusion_cowork.server.mcp_http import create_http_app
        from fusion_cowork.server.mcp_server import MCPToolRegistry

        registry = MCPToolRegistry()
        registry.register_tools()
        return create_http_app(registry)

    @pytest.fixture
    def client(self, app):
        from httpx import ASGITransport, AsyncClient

        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    @pytest.mark.asyncio
    async def test_rpc_parse_error(self, client):
        async with client as c:
            resp = await c.post("/rpc", content=b"{not json", headers={"Content-Type": "application/json"})
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == -32700

    @pytest.mark.asyncio
    async def test_rpc_rejects_non_plugins(self, client):
        async with client as c:
            resp = await c.post("/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            assert resp.status_code == 500
            assert resp.json()["error"]["code"] == -32601

    @pytest.mark.asyncio
    @pytest.mark.skipif(not is_plugins_available(), reason="需要 fusion-plugins-ecosystem")
    async def test_rpc_plugins_ping(self, client):
        async with client as c:
            resp = await c.post("/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "plugins.ping", "params": {}})
            assert resp.status_code == 200
            body = resp.json()
            assert body["jsonrpc"] == "2.0"
            assert body["id"] == 1
            assert body["result"] == {"pong": True}

    @pytest.mark.asyncio
    @pytest.mark.skipif(not is_plugins_available(), reason="需要 fusion-plugins-ecosystem")
    async def test_rpc_plugins_list(self, client):
        async with client as c:
            resp = await c.post("/rpc", json={"jsonrpc": "2.0", "id": 2, "method": "plugins/list", "params": {}})
            assert resp.status_code == 200
            assert "plugins" in resp.json()["result"]

    @pytest.mark.asyncio
    async def test_rpc_health_still_works(self, client):
        async with client as c:
            resp = await c.get("/health")
            assert resp.status_code == 200
            # v0.4.0 Stage 4: 深度 /health, 上游未起 → degraded 不阻断
            assert resp.json()["status"] in ("ok", "degraded")
