"""issue #206: http_request 节点 — DAG 内调用外部 REST API。

MockTransport 离线桩: 无真实网络, 校验方法/请求体/模板解析/非 2xx 失败语义;
另覆盖注册、HIGH_RISK 名单、url: scope 权限。
"""

from __future__ import annotations

import json
import logging

import httpx

from fusion_cowork.engine.node import NodeConfig, NodeRegistry, coerce_params
from fusion_cowork.engine.permission_manager import HIGH_RISK_NODES, Permission, PermissionManager
from fusion_cowork.nodes.tools.http_request_node import (
    HttpRequestNode,
    _lookup,
    resolve_template,
)


def _make_node(params=None):
    return HttpRequestNode(config=NodeConfig(params=params or {}))


def _handler(payload, status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["request"] = request
            seen["body"] = request.content
        return httpx.Response(status, json=payload, headers={"X-Stub": "1"})

    return handler


def _bind(node, handler):
    node._client = lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)


class TestTemplateResolution:
    def test_nested_dict_and_list(self):
        ctx = {"task": {"id": 42, "tags": ["a", "b"]}, "name": "张三"}
        out = resolve_template(
            {"id": "${task.id}", "list": ["${name}", "${task.tags}"], "keep": 7},
            ctx,
        )
        assert out["id"] == "42"
        assert out["list"][0] == "张三"
        assert json.loads(out["list"][1]) == ["a", "b"]
        assert out["keep"] == 7

    def test_unresolved_template_kept_literal_and_warned(self, caplog):
        with caplog.at_level(logging.WARNING, logger="fusion_cowork.nodes.tools.http_request_node"):
            out = resolve_template("Bearer ${missing.token}", {"a": 1})
        assert out == "Bearer ${missing.token}"
        assert any("模板变量未命中" in r.message for r in caplog.records)

    def test_lookup_scalar_dict_list(self):
        assert _lookup("a", {"a": 1}, "${a}") == "1"
        assert json.loads(_lookup("a", {"a": {"x": 1}}, "${a}")) == {"x": 1}
        assert _lookup("a.b.c", {"a": {}}, "${a.b.c}") == "${a.b.c}"

    def test_non_string_passthrough(self):
        assert resolve_template(3, {}) == 3
        assert resolve_template(None, {}) is None
        assert resolve_template(True, {}) is True


class TestHttpRequestExecution:
    async def test_post_json_success(self):
        seen = {}
        node = _make_node({"url": "http://127.0.0.1:11480/api", "method": "POST", "body": {"task": "${task_id}"}})
        _bind(node, _handler({"ok": True}, seen=seen))
        result = await node.execute({"task_id": "t-9"})
        assert result.status.value == "success"
        assert result.data["status_code"] == 200
        assert result.data["json"] == {"ok": True}
        assert result.data["headers"]["x-stub"] == "1"
        assert json.loads(seen["body"]) == {"task": "t-9"}

    async def test_get_without_body(self):
        seen = {}
        node = _make_node({"url": "http://127.0.0.1:11480/api", "method": "GET"})
        _bind(node, _handler({"rows": []}, seen=seen))
        result = await node.execute({})
        assert result.status.value == "success"
        assert seen["body"] == b""

    async def test_headers_merge_inputs_override_params(self):
        seen = {}
        node = _make_node(
            {"url": "http://127.0.0.1:11480/api", "method": "GET", "headers": {"Authorization": "Bearer p", "X-A": "p"}}
        )
        _bind(node, _handler({"ok": 1}, seen=seen))
        await node.execute({"headers": {"Authorization": "Bearer ${tok}", "X-B": "b"}, "tok": "real"})
        req = seen["request"]
        assert req.headers["Authorization"] == "Bearer real"
        assert req.headers["X-A"] == "p"
        assert req.headers["X-B"] == "b"

    async def test_non_2xx_is_node_failure_with_body(self):
        node = _make_node({"url": "http://127.0.0.1:11480/api"})
        _bind(node, _handler({"error": "boom"}, status=500))
        result = await node.execute({})
        assert result.status.value == "failed"
        assert "HTTP 500" in result.error
        assert "boom" in result.error
        assert result.data["status_code"] == 500

    async def test_string_body_is_json_encoded(self):
        seen = {}
        node = _make_node({"url": "http://127.0.0.1:11480/api", "body": '{"a": "${v}"}'})
        _bind(node, _handler({"ok": 1}, seen=seen))
        await node.execute({"v": "1"})
        assert json.loads(seen["body"]) == {"a": "1"}
        assert seen["request"].headers["content-type"] == "application/json"

    async def test_plain_text_body_uses_content(self):
        seen = {}
        node = _make_node({"url": "http://127.0.0.1:11480/api", "body": "raw-text-${v}"})
        _bind(node, _handler({"ok": 1}, seen=seen))
        await node.execute({"v": "x"})
        assert seen["body"] == b"raw-text-x"

    async def test_non_json_response_yields_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        node = _make_node({"url": "http://127.0.0.1:11480/api"})
        _bind(node, handler)
        result = await node.execute({})
        assert result.status.value == "success"
        assert result.data["json"] is None
        assert result.data["text"] == "not json"

    async def test_missing_and_illegal_url(self):
        node = _make_node({})
        r1 = await node.execute({})
        assert r1.status.value == "failed"
        assert "缺少 url" in r1.error
        node2 = _make_node({"url": "file:///etc/passwd"})
        r2 = await node2.execute({})
        assert r2.status.value == "failed"
        assert "非法 URL" in r2.error

    async def test_unsupported_method(self):
        node = _make_node({"url": "http://127.0.0.1:11480/api", "method": "TRACE"})
        result = await node.execute({})
        assert result.status.value == "failed"
        assert "不支持的 HTTP 方法" in result.error

    async def test_timeout_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        node = _make_node({"url": "http://127.0.0.1:11480/api"})
        _bind(node, handler)
        result = await node.execute({})
        assert result.status.value == "failed"
        assert "超时" in result.error

    async def test_connection_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        node = _make_node({"url": "http://127.0.0.1:1/api"})
        _bind(node, handler)
        result = await node.execute({})
        assert result.status.value == "failed"
        assert "连接失败" in result.error

    async def test_inputs_url_overrides_params(self):
        seen = {}
        node = _make_node({"url": "http://127.0.0.1:11480/from-params", "method": "GET"})
        _bind(node, _handler({"ok": 1}, seen=seen))
        await node.execute({"url": "http://127.0.0.1:11480/from-inputs"})
        assert str(seen["request"].url) == "http://127.0.0.1:11480/from-inputs"

    def test_coerce_string_params(self):
        params = coerce_params(
            {"url": "http://x", "method": "get", "timeout": "5"},
            HttpRequestNode(config=NodeConfig(params={})).get_params_schema(),
        )
        assert params["timeout"] == 5.0


class TestRegistrationAndPermissions:
    def test_registered_via_import_all_nodes(self):
        from fusion_cowork.nodes import import_all_nodes

        import_all_nodes()
        assert NodeRegistry.get("http_request") is not None

    def test_high_risk_listing(self):
        assert "http_request" in HIGH_RISK_NODES

    async def test_url_scope_permission_matching(self):
        pm = PermissionManager()
        pm.approve("http_request", scope="url:http://127.0.0.1:11480/**")
        assert await pm.check("http_request", "execute", {"url": "http://127.0.0.1:11480/api"}) is True
        assert await pm.check("http_request", "execute", {"url": "http://127.0.0.1:9999/api"}) is False
        assert await pm.check("shell_exec", "execute") is False

    def test_permission_url_scope_unit(self):
        rule = Permission(tool_name="http_request", allowed=True, scope="url:http://127.0.0.1:11480/**")
        assert rule.matches("http_request", {"url": "http://127.0.0.1:11480/api"})
        assert not rule.matches("http_request", {"url": "http://127.0.0.1:11481/api"})
        assert not rule.matches("shell_exec", {"url": "http://127.0.0.1:11480/api"})

    async def test_high_risk_unapproved_deny(self):
        pm = PermissionManager()
        assert await pm.check("http_request", "execute", {"url": "http://127.0.0.1:11480/api"}) is False
