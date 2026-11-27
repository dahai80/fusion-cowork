"""issue #185: MCP client node — invoke external MCP tool servers (Streamable HTTP) inside a DAG.

One tool call per node: initialize handshake -> reuse Mcp-Session-Id -> tools/call.
High-risk node (remote tool execution); permission rules support url:-prefixed
scopes to authorize a single server precisely.
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

# issue #214: degraded-mode warning when fusion-core is absent is emitted once per process
_registry_skip_warned = False


@register_node
class MCPClientNode(BaseNode):
    name = "mcp_client"
    # i18n: user-facing
    display_name = "MCP 客户端"
    category = NodeCategory.FUSION_ECOSYSTEM
    # i18n: user-facing
    description = "调用外部 MCP 服务器的工具 (Streamable HTTP): initialize 握手 + tools/call"
    icon = "🔌"
    # i18n: user-facing
    default_label = "MCP 工具调用"

    inputs = [
        # i18n: user-facing
        {"key": "url", "label": "MCP 服务器 URL (transport=http)", "type": "string"},
        # i18n: user-facing
        {"key": "tool", "label": "工具名", "type": "string"},
        # i18n: user-facing
        {"key": "arguments", "label": "工具参数 (JSON 对象)", "type": "object"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "result", "label": "工具返回文本", "type": "string"},
        # i18n: user-facing
        {"key": "raw", "label": "原始 content 数组", "type": "array"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "url": {"type": "string", "description": "MCP 服务器 URL (http/https; transport=http 时必填)"},
                # i18n: user-facing
                "tool": {"type": "string", "description": "要调用的工具名"},
                # i18n: user-facing
                "arguments": {"type": "object", "description": "工具参数", "default": {}},
                "transport": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "传输方式: http (缺省, Streamable HTTP) | stdio (子进程 JSON-RPC, 需 FUSION_COWORK_MCP_STDIO_ALLOW 白名单)",
                    "enum": ["http", "stdio"],
                    "default": "http",
                },
                "command": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "stdio 模式: 可执行文件 (basename 须在 FUSION_COWORK_MCP_STDIO_ALLOW 白名单)",
                    "default": "",
                },
                # i18n: user-facing
                "args": {"type": "array", "description": "stdio 模式: 命令行参数", "default": []},
                # i18n: user-facing
                "headers": {"type": "object", "description": "附加请求头 (如鉴权, http 模式)", "default": {}},
                # i18n: user-facing
                "timeout": {"type": "number", "description": "单请求超时 (秒)", "default": 30},
            },
            "required": ["url", "tool"],
        }

    def _client(self, timeout: float) -> httpx.AsyncClient:
        # test seam: subclasses/monkeypatch can inject a MockTransport
        return httpx.AsyncClient(timeout=timeout)

    def _registry_check(self, url: str, tool: str) -> None:
        # issue #210: audit external MCP calls against architecture/mcp-server-registry.json.
        # warn level, never blocking (local-first: our own services take priority), verdict +
        # correlation_id land in the audit log;
        # issue #214: when fusion-core is absent (bare CI install) emit a WARNING about audit
        # degradation (once per process, to avoid CI log flooding).
        global _registry_skip_warned
        try:
            from fusion_core import check_approval
        except ImportError:
            if not _registry_skip_warned:
                _registry_skip_warned = True
                logger.warning(f"mcp_client: fusion_core.mcp_registry unavailable, registry audit degraded/skipped url={url} tool={tool}")
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
            logger.warning(f"mcp_client: registry check failed {url} tool={tool} correlation_id={correlation_id}: {e}")
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
        # Streamable HTTP servers may wrap the JSON-RPC response in SSE frames — parse both content-types
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

    async def _execute_stdio(
        self, command: str, args: list, tool: str, arguments: Dict[str, Any], timeout: float
    ) -> NodeResult:
        # issue #431: stdio transport — subprocess JSON-RPC; the command must be inside the
        # FUSION_COWORK_MCP_STDIO_ALLOW allowlist (exact basename match, empty default =
        # reject all, fail-closed)
        if not command or not tool:
            return NodeResult(status=NodeStatus.FAILED, error="stdio mode missing command or tool param", summary="missing params")
        from fusion_cowork.nodes.ecosystem.mcp_stdio import (
            STDIO_ALLOW_ENV,
            stdio_allowed,
            stdio_allowlist,
            stdio_tool_calls,
        )

        if not stdio_allowed(command):
            allowed = ", ".join(stdio_allowlist()) or "(empty — reject all)"
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"command {command} is not in the {STDIO_ALLOW_ENV} allowlist (current: {allowed})",
                summary="stdio command not authorized",
            )
        logger.info(f"mcp_client: stdio call command={command} tool={tool}")
        try:
            results = await stdio_tool_calls(command, args, [{"tool": tool, "arguments": arguments}], timeout=timeout)
        except Exception as e:
            logger.warning(f"mcp_client: stdio call failed command={command} tool={tool}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"stdio MCP call failed: {e}", summary="call failed")
        first = results[0] if results else {"text": "", "content": []}
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"result": first["text"], "raw": first["content"]},
            summary=f"MCP stdio tool {tool} call succeeded",
        )

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = coerce_params(self.config.params, self.get_params_schema())
        url = str(inputs.get("url") or params.get("url") or "").strip()
        tool = str(inputs.get("tool") or params.get("tool") or "").strip()
        arguments = inputs.get("arguments", params.get("arguments", {}))
        extra_headers = params.get("headers", {}) or {}
        timeout = float(params.get("timeout", 30) or 30)
        transport = str(params.get("transport") or "http").strip().lower()
        stdio_command = str(params.get("command") or "").strip()
        stdio_args = params.get("args") or []

        if transport == "stdio":
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments.strip())
                except json.JSONDecodeError as e:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"arguments must be a JSON object (JSON string parse failed: {e})",
                        summary="invalid params",
                    )
            if not isinstance(arguments, dict):
                return NodeResult(status=NodeStatus.FAILED, error="arguments must be a JSON object", summary="invalid params")
            return await self._execute_stdio(stdio_command, stdio_args, tool, arguments, timeout)

        if not url or not tool:
            return NodeResult(status=NodeStatus.FAILED, error="missing url or tool param", summary="missing params")
        if not url.startswith(("http://", "https://")):
            return NodeResult(status=NodeStatus.FAILED, error=f"invalid URL: {url}", summary="invalid params")
        if isinstance(arguments, str):
            # issue #211: upstream mcp_client output is text, so this node receives a JSON string
            # across the edge — parse it and continue
            raw_arguments = arguments
            try:
                arguments = json.loads(raw_arguments.strip())
            except json.JSONDecodeError as e:
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"arguments must be a JSON object (JSON string parse failed: {e})",
                    summary="invalid params",
                )
            logger.info(f"mcp_client: arguments parsed from JSON string tool={tool} chars={len(raw_arguments)}")
        if not isinstance(arguments, dict):
            return NodeResult(status=NodeStatus.FAILED, error="arguments must be a JSON object", summary="invalid params")

        base_headers = {"Accept": "application/json, text/event-stream", **extra_headers}
        # issue #210: run the registry audit before the handshake (warn level, never blocking)
        self._registry_check(url, tool)
        logger.info(f"mcp_client: calling {url} tool={tool}")
        try:
            async with self._client(timeout) as client:
                # 1) initialize handshake -> server issues Mcp-Session-Id
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
                        error=f"initialize failed: HTTP {init_resp.status_code} {init_resp.text[:200]}",
                        summary="handshake failed",
                    )
                init_body = self._parse_reply(init_resp)
                if "error" in init_body:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"initialize failed: {init_body['error']}",
                        summary="handshake failed",
                    )
                session_id = init_resp.headers.get("mcp-session-id", "")

                # 2) notifications/initialized (server 202 counts as success, body ignored)
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
                        error=f"tools/call failed: HTTP {call_resp.status_code} {call_resp.text[:200]}",
                        summary="call failed",
                    )
                body = self._parse_reply(call_resp)
                if body.get("error"):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"tools/call error: {body['error']}",
                        summary="call failed",
                    )
                result = body.get("result") or {}
                content = result.get("content") or []
                text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                if result.get("isError"):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error="; ".join(t for t in text_parts if t) or "tool execution reported an error",
                        data={"raw": content},
                        summary="tool error",
                    )
                logger.info(f"mcp_client: {tool} call succeeded ({len(text_parts)} text segments)")
                return NodeResult(
                    status=NodeStatus.SUCCESS,
                    data={"result": "\n".join(text_parts), "raw": content},
                    summary=f"MCP tool {tool} call succeeded",
                )
        except httpx.TimeoutException as e:
            logger.warning(f"mcp_client: timed out {url}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"MCP call timed out: {e}", summary="call timed out")
        except httpx.HTTPError as e:
            logger.warning(f"mcp_client: connection failed {url}: {e}")
            return NodeResult(status=NodeStatus.FAILED, error=f"MCP connection failed: {e}", summary="connection failed")
