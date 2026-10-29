"""issue #206: 通用 HTTP 请求节点 — DAG 内调用任意外部 REST API。

一节点一次请求: method/url/headers/body, 请求体支持 ${dotted.path} 模板
(从上游输入/首节点注入解析); 响应 JSON 进 step 输出供下游消费;
非 2xx 映射为节点失败 (run 状态对账成立)。
高危节点 (任意目标请求面), 权限规则支持 url: 前缀 scope 精确授权。
本节点面向本机/内网自家服务 (local-first), 与 fetch_url 的公网 SSRF 策略不同,
不做私网拦截 — 由 HIGH_RISK 默认拒 + url: scope 显式放行控制。
"""

import json
import logging
import re
from typing import Any, Dict

import httpx

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)

logger = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}")
_MAX_BODY_SNIPPET = 500


def resolve_template(value: Any, context: Dict[str, Any]) -> Any:
    # 递归解析 url/headers/body 中的 ${dotted.path} — 从上游输入 (含首节点注入)
    # 逐级取值; 未命中保留字面量并记 WARNING (调用方可观测漏配)。
    if isinstance(value, str):
        return _TEMPLATE_RE.sub(lambda m: _lookup(m.group(1), context, m.group(0)), value)
    if isinstance(value, dict):
        return {k: resolve_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_template(v, context) for v in value]
    return value


def _lookup(path: str, context: Dict[str, Any], literal: str) -> str:
    node: Any = context
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            logger.warning(f"http_request: 模板变量未命中, 保留字面量: {literal}")
            return literal
        node = node[part]
    if isinstance(node, (dict, list)):
        return json.dumps(node, ensure_ascii=False)
    return str(node)


@register_node
class HttpRequestNode(BaseNode):
    name = "http_request"
    display_name = "HTTP 请求"
    category = NodeCategory.TOOL
    description = "发起任意 HTTP 请求 (method/url/headers/body), 响应 JSON 供下游节点消费"
    icon = "🌐"
    default_label = "HTTP 请求"

    inputs = [
        {"key": "url", "label": "URL", "type": "string"},
        {"key": "method", "label": "HTTP 方法", "type": "string"},
        {"key": "headers", "label": "请求头 (JSON 对象)", "type": "object"},
        {"key": "body", "label": "请求体 (JSON 对象或字符串)", "type": "object"},
    ]
    outputs = [
        {"key": "status_code", "label": "响应状态码", "type": "integer"},
        {"key": "json", "label": "响应 JSON (解析失败为 None)", "type": "object"},
        {"key": "text", "label": "响应文本", "type": "string"},
        {"key": "headers", "label": "响应头", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "POST"},
                "url": {"type": "string", "description": "目标 URL (http/https)"},
                "headers": {"type": "object", "description": "请求头 (如鉴权)", "default": {}},
                "body": {"description": "请求体 (对象或 JSON 字符串), 支持 ${path} 模板", "default": {}},
                "timeout": {"type": "number", "description": "超时秒数", "default": 30},
            },
            "required": ["url"],
        }

    def _client(self, timeout: float) -> httpx.AsyncClient:
        # 测试接缝: 子类/monkeypatch 可注入 MockTransport
        return httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        url = str(inputs.get("url") or params.get("url") or "").strip()
        method = str(inputs.get("method") or params.get("method") or "POST").strip().upper()
        headers = {**(params.get("headers") or {}), **(inputs.get("headers") or {})}
        body = inputs.get("body", params.get("body", {}))
        timeout = float(params.get("timeout", 30) or 30)

        if not url:
            return NodeResult(status=NodeStatus.FAILED, error="缺少 url 参数", summary="参数缺失")
        if not url.startswith(("http://", "https://")):
            return NodeResult(status=NodeStatus.FAILED, error=f"非法 URL: {url}", summary="参数非法")
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return NodeResult(status=NodeStatus.FAILED, error=f"不支持的 HTTP 方法: {method}", summary="参数非法")

        headers = resolve_template(headers, inputs)
        body = resolve_template(body, inputs)
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                pass

        request_kwargs: Dict[str, Any] = {"headers": headers}
        if method in ("POST", "PUT", "PATCH") and body not in (None, {}, ""):
            request_kwargs["json"] = body if isinstance(body, (dict, list)) else None
            if not isinstance(body, (dict, list)):
                request_kwargs["content"] = str(body)

        logger.info(
            f"http_request: {method} {url} (body={'有' if 'json' in request_kwargs or 'content' in request_kwargs else '无'})"
        )
        try:
            async with self._client(timeout) as client:
                resp = await client.request(method, url, **request_kwargs)
        except httpx.TimeoutException as e:
            logger.warning(f"http_request: 超时 {method} {url}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"HTTP 请求超时: {e}", summary="请求超时")
        except httpx.HTTPError as e:
            logger.warning(f"http_request: 连接失败 {method} {url}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"HTTP 连接失败: {e}", summary="连接失败")

        text = resp.text
        try:
            resp_json: Any = resp.json()
        except (json.JSONDecodeError, ValueError):
            resp_json = None

        if resp.status_code < 200 or resp.status_code >= 300:
            snippet = text[:_MAX_BODY_SNIPPET]
            logger.warning(f"http_request: 非 2xx 响应 {method} {url} status={resp.status_code}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"HTTP {resp.status_code}: {snippet}",
                data={"status_code": resp.status_code, "text": text, "json": resp_json},
                summary=f"HTTP {resp.status_code}",
            )

        logger.info(f"http_request: {method} {url} 成功 status={resp.status_code} ({len(text)} chars)")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "status_code": resp.status_code,
                "json": resp_json,
                "text": text,
                "headers": dict(resp.headers),
            },
            summary=f"HTTP {resp.status_code} 成功",
        )
