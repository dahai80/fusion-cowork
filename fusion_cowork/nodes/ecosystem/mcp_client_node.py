"""issue #185: MCP client 节点 — DAG 内调用外部 MCP 工具服务器 (Streamable HTTP)。

一节点一次工具调用: initialize 握手 → 复用 Mcp-Session-Id → tools/call。
高危节点 (远程工具执行), 权限规则支持 url: 前缀 scope 精确授权单个服务。
"""

import json
import logging
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

_JSONRPC_VERSION = "2025-03-26"
_PROPOSED_MCP_VERSION = "2025-03-26"

# issue #214: fusion-core 缺席时降级警告每进程只发一次
_registry_skip_warned = False


@register_node
class MCPClientNode(BaseNode):
    name = "mcp_client"
    display_name = "MCP 客户端"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "调用外部 MCP 服务器的工具 (Streamable HTTP): initialize 握手 + tools/call"
    icon = "🔌"
    default_label = "MCP 工具调用"

    inputs = [
        {"key": "url", "label": "MCP 服务器 URL", "type": "string"},
        {"key": "tool", "label": "工具名", "type": "string"},
        {"key": "arguments", "label": "工具参数 (JSON 对象)", "type": "object"},
    ]
    outputs = [
        {"key": "result", "label": "工具返回文本", "type": "string"},
        {"key": "raw", "label": "原始 content 数组", "type": "array"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "MCP 服务器 URL (http/https)"},
                "tool": {"type": "string", "description": "要调用的工具名"},
                "arguments": {"type": "object", "description": "工具参数", "default": {}},
                "headers": {"type": "object", "description": "附加请求头 (如鉴权)", "default": {}},
                "timeout": {"type": "number", "description": "单请求超时 (秒)", "default": 30},
            },
            "required": ["url", "tool"],
        }

    def _client(self, timeout: float) -> httpx.AsyncClient:
        # 测试接缝: 子类/monkeypatch 可注入 MockTransport
        return httpx.AsyncClient(timeout=timeout)

    def _registry_check(self, url: str, tool: str) -> None:
        # issue #210: 对照 architecture/mcp-server-registry.json 审计外部 MCP 调用。
        # warn 级不阻断 (local-first 自家服务优先可用), verdict + correlation_id 落审计日志;
        # issue #214: fusion-core 缺席 (CI 裸装) 时 WARNING 提示审计降级 (每进程一次, 防 CI 循环刷屏)。
        global _registry_skip_warned
        try:
            from fusion_core import check_approval
        except ImportError:
            if not _registry_skip_warned:
                _registry_skip_warned = True
                logger.warning(f"mcp_client: fusion_core.mcp_registry 不可用, 注册表审计降级跳过 url={url} tool={tool}")
            return
        correlation_id = ""
        try:
            from fusion_cowork.observability.trace import get_trace_id

            correlation_id = get_trace_id() or ""
        except Exception:
            correlation_id = ""
        try:
            verdict = check_approval(url, tool_scope=tool)
        except Exception as e:
            logger.warning(f"mcp_client: 注册表检查失败 {url} tool={tool} correlation_id={correlation_id}: {e}")
            return
        if verdict.action == "approved":
            logger.info(
                f"mcp_client: registry approved server={verdict.server} tool={tool} correlation_id={correlation_id}"
            )
        else:
            logger.warning(
                f"mcp_client: registry {verdict.action} server={verdict.server} tool={tool} "
                f"reason={verdict.reason} correlation_id={correlation_id}"
            )

    @staticmethod
    def _parse_reply(resp: httpx.Response) -> Dict[str, Any]:
        # Streamable HTTP 服务器可能以 SSE 帧回 JSON-RPC 响应 — 两种 content-type 都解析
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            payload: Dict[str, Any] = {}
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        payload = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
            return payload
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return {}

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        url = str(inputs.get("url") or params.get("url") or "").strip()
        tool = str(inputs.get("tool") or params.get("tool") or "").strip()
        arguments = inputs.get("arguments", params.get("arguments", {}))
        extra_headers = params.get("headers", {}) or {}
        timeout = float(params.get("timeout", 30) or 30)

        if not url or not tool:
            return NodeResult(status=NodeStatus.FAILED, error="缺少 url 或 tool 参数", summary="参数缺失")
        if not url.startswith(("http://", "https://")):
            return NodeResult(status=NodeStatus.FAILED, error=f"非法 URL: {url}", summary="参数非法")
        if isinstance(arguments, str):
            # issue #211: 上游 mcp_client 输出是文本, 连边后本节点收到 JSON 字符串 — 解析后继续
            raw_arguments = arguments
            try:
                arguments = json.loads(raw_arguments.strip())
            except json.JSONDecodeError as e:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"arguments 须为 JSON 对象 (JSON 字符串解析失败: {e})",
                    summary="参数非法",
                )
            logger.info(f"mcp_client: arguments 由 JSON 字符串解析 tool={tool} chars={len(raw_arguments)}")
        if not isinstance(arguments, dict):
            return NodeResult(status=NodeStatus.FAILED, error="arguments 须为 JSON 对象", summary="参数非法")

        base_headers = {"Accept": "application/json, text/event-stream", **extra_headers}
        # issue #210: 握手前先做注册表审计 (warn 级, 不阻断)
        self._registry_check(url, tool)
        logger.info(f"mcp_client: 调用 {url} tool={tool}")
        try:
            async with self._client(timeout) as client:
                # 1) initialize 握手 → 服务端下发 Mcp-Session-Id
                init_resp = await client.post(
                    url,
                    headers=base_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _PROPOSED_MCP_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "fusion-cowork-mcp-client", "version": "1.0"},
                        },
                    },
                )
                if init_resp.status_code != 200:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"initialize 失败: HTTP {init_resp.status_code} {init_resp.text[:200]}",
                        summary="握手失败",
                    )
                init_body = self._parse_reply(init_resp)
                if "error" in init_body:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"initialize 失败: {init_body['error']}",
                        summary="握手失败",
                    )
                session_id = init_resp.headers.get("mcp-session-id", "")

                # 2) notifications/initialized (服务端 202 即成功, 忽略响应体)
                req_headers = dict(base_headers)
                if session_id:
                    req_headers["Mcp-Session-Id"] = session_id
                await client.post(
                    url,
                    headers=req_headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )

                # 3) tools/call
                call_resp = await client.post(
                    url,
                    headers=req_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    },
                )
                if call_resp.status_code != 200:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"tools/call 失败: HTTP {call_resp.status_code} {call_resp.text[:200]}",
                        summary="调用失败",
                    )
                body = self._parse_reply(call_resp)
                if body.get("error"):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"tools/call 错误: {body['error']}",
                        summary="调用失败",
                    )
                result = body.get("result") or {}
                content = result.get("content") or []
                text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                if result.get("isError"):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error="; ".join(t for t in text_parts if t) or "工具执行报错",
                        data={"raw": content},
                        summary="工具报错",
                    )
                logger.info(f"mcp_client: {tool} 调用成功 ({len(text_parts)} 段文本)")
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"result": "\n".join(text_parts), "raw": content},
                    summary=f"MCP 工具 {tool} 调用成功",
                )
        except httpx.TimeoutException as e:
            logger.warning(f"mcp_client: 超时 {url}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"MCP 调用超时: {e}", summary="调用超时")
        except httpx.HTTPError as e:
            logger.warning(f"mcp_client: 连接失败 {url}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"MCP 连接失败: {e}", summary="连接失败")
