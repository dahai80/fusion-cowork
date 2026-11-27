"""issue #206: generic HTTP request node — call any external REST API from the DAG.

One request per node: method/url/headers/body; the request body supports ${dotted.path} templates
(resolved from upstream inputs / first-node injection); the response JSON lands in step output for downstream consumption;
non-2xx maps to node failure (run status reconciliation holds).
High-risk node (arbitrary-target request surface); permission rules support url:-prefix scopes for precise authorization.
This node targets local/intranet first-party services (local-first); unlike fetch_url's public-internet SSRF policy,
it does no private-network blocking — controlled by HIGH_RISK default-deny + explicit url: scope allow.
"""

import json
import logging
import os
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
    # Recursively resolve ${dotted.path} in url/headers/body — values fetched level by level
    # from upstream inputs (including first-node injection); ${env.VAR} reads the process env (issue #287: template auth headers are not hardcoded);
    # on a miss the literal is kept and a WARNING is logged (missed config is observable to the caller).
    if isinstance(value, str):
        return _TEMPLATE_RE.sub(lambda m: _lookup(m.group(1), context, m.group(0)), value)
    if isinstance(value, dict):
        return {k: resolve_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_template(v, context) for v in value]
    return value


def _lookup(path: str, context: Dict[str, Any], literal: str) -> str:
    # issue #287: the ${env.VAR} prefix — sensitive values such as auth headers are injected from the environment, never as template plaintext
    if path.startswith("env."):
        env_val = os.environ.get(path[4:], "")
        if env_val:
            return env_val
        logger.warning(f"http_request: env variable miss, keeping the literal: {literal}")
        return literal
    node: Any = context
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            logger.warning(f"http_request: template variable miss, keeping the literal: {literal}")
            return literal
        node = node[part]
    if isinstance(node, (dict, list)):
        return json.dumps(node, ensure_ascii=False)
    return str(node)


@register_node
class HttpRequestNode(BaseNode):
    name = "http_request"
    # i18n: user-facing
    display_name = "HTTP 请求"
    category = NodeCategory.TOOL
    # i18n: user-facing
    description = "发起任意 HTTP 请求 (method/url/headers/body), 响应 JSON 供下游节点消费"
    icon = "🌐"
    # i18n: user-facing
    default_label = "HTTP 请求"

    inputs = [
        {"key": "url", "label": "URL", "type": "string"},
        # i18n: user-facing
        {"key": "method", "label": "HTTP 方法", "type": "string"},
        # i18n: user-facing
        {"key": "headers", "label": "请求头 (JSON 对象)", "type": "object"},
        # i18n: user-facing
        {"key": "body", "label": "请求体 (JSON 对象或字符串)", "type": "object"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "status_code", "label": "响应状态码", "type": "integer"},
        # i18n: user-facing
        {"key": "json", "label": "响应 JSON (解析失败为 None)", "type": "object"},
        # i18n: user-facing
        {"key": "text", "label": "响应文本", "type": "string"},
        # i18n: user-facing
        {"key": "headers", "label": "响应头", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "POST"},
                # i18n: user-facing
                "url": {"type": "string", "description": "目标 URL (http/https)"},
                # i18n: user-facing
                "headers": {"type": "object", "description": "请求头 (如鉴权)", "default": {}},
                # i18n: user-facing
                "body": {"description": "请求体 (对象或 JSON 字符串), 支持 ${path} 模板", "default": {}},
                # i18n: user-facing
                "timeout": {"type": "number", "description": "超时秒数", "default": 30},
            },
            "required": ["url"],
        }

    def _client(self, timeout: float) -> httpx.AsyncClient:
        # Test seam: subclasses/monkeypatch can inject a MockTransport
        return httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        url = str(inputs.get("url") or params.get("url") or "").strip()
        method = str(inputs.get("method") or params.get("method") or "POST").strip().upper()
        headers = {**(params.get("headers") or {}), **(inputs.get("headers") or {})}
        body = inputs.get("body", params.get("body", {}))
        timeout = float(params.get("timeout", 30) or 30)

        if not url:
            return NodeResult(status=NodeStatus.FAILED, error="Missing url param", summary="Missing params")
        if not url.startswith(("http://", "https://")):
            return NodeResult(status=NodeStatus.FAILED, error=f"Invalid URL: {url}", summary="Bad params")
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return NodeResult(status=NodeStatus.FAILED, error=f"Unsupported HTTP method: {method}", summary="Bad params")

        headers = resolve_template(headers, inputs)
        # issue #287: auth header safety — headers still containing ${...} after resolution (env unset) are dropped outright,
        # never sending the template literal as a credential; body keeps the literal (backward compatible, observable).
        dropped = [k for k, v in headers.items() if isinstance(v, str) and "${" in v]
        for k in dropped:
            headers.pop(k, None)
            logger.warning(f"http_request: auth header template unresolved, header dropped: {k}")
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
            f"http_request: {method} {url} (body={'yes' if 'json' in request_kwargs or 'content' in request_kwargs else 'no'})"
        )
        try:
            async with self._client(timeout) as client:
                resp = await client.request(method, url, **request_kwargs)
        except httpx.TimeoutException as e:
            logger.warning(f"http_request: timeout {method} {url}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"HTTP request timed out: {e}", summary="Request timed out")
        except httpx.HTTPError as e:
            logger.warning(f"http_request: connection failed {method} {url}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"HTTP connection failed: {e}", summary="Connection failed")

        text = resp.text
        try:
            resp_json: Any = resp.json()
        except (json.JSONDecodeError, ValueError):
            resp_json = None

        if resp.status_code < 200 or resp.status_code >= 300:
            snippet = text[:_MAX_BODY_SNIPPET]
            logger.warning(f"http_request: non-2xx response {method} {url} status={resp.status_code}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"HTTP {resp.status_code}: {snippet}",
                data={"status_code": resp.status_code, "text": text, "json": resp_json},
                summary=f"HTTP {resp.status_code}",
            )

        logger.info(f"http_request: {method} {url} succeeded status={resp.status_code} ({len(text)} chars)")
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "status_code": resp.status_code,
                "json": resp_json,
                "text": text,
                "headers": dict(resp.headers),
            },
            summary=f"HTTP {resp.status_code} succeeded",
        )
