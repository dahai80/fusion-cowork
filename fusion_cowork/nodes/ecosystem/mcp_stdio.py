"""issue #431: stdio MCP client helper — newline-delimited JSON-RPC over a subprocess.

Shared by mcp_client (transport=stdio) and design_context. Spawn is gated by
FUSION_COWORK_MCP_STDIO_ALLOW (comma/semicolon-separated binary basenames,
exact match on os.path.basename). Empty/unset allowlist = deny all (fail-closed):
a workflow author cannot make the engine spawn arbitrary processes implicitly.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

STDIO_ALLOW_ENV = "FUSION_COWORK_MCP_STDIO_ALLOW"
_MCP_PROTOCOL_VERSION = "2025-03-26"


def stdio_allowlist() -> List[str]:
    raw = os.environ.get(STDIO_ALLOW_ENV, "")
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def stdio_allowed(command: str) -> bool:
    allowed = stdio_allowlist()
    if not allowed:
        return False
    return os.path.basename(str(command).strip()) in allowed


async def stdio_tool_calls(
    command: str,
    args: List[Any],
    calls: List[Dict[str, Any]],
    timeout: float = 60.0,
) -> List[Dict[str, Any]]:
    # Spawn the MCP stdio server, initialize handshake, run tools/call sequence,
    # return [{"tool", "text", "content", "isError"}]. Raises RuntimeError on any
    # protocol error; the subprocess is always torn down.
    proc = await asyncio.create_subprocess_exec(
        command,
        *[str(a) for a in (args or [])],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        # DEVNULL: a chatty stderr must not fill the pipe and deadlock the server
        stderr=asyncio.subprocess.DEVNULL,
    )
    results: List[Dict[str, Any]] = []

    async def _request(req_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        if not raw:
            raise RuntimeError(f"stdio MCP server closed stdout early (req id={req_id}, command={command})")
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"stdio MCP server sent non-JSON reply (req id={req_id}): {e}") from e
        if body.get("error"):
            raise RuntimeError(f"stdio MCP error (req id={req_id}): {body['error']}")
        return body.get("result") or {}

    try:
        init = await _request(
            1,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "fusion-cowork-mcp-client", "version": "1.0"},
                },
            },
        )
        server_info = json.dumps(init.get("serverInfo") or {}, ensure_ascii=False)
        logger.info(f"stdio MCP initialized command={command} server={server_info}")
        note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        proc.stdin.write(note.encode("utf-8") + b"\n")
        await proc.stdin.drain()

        for i, call in enumerate(calls):
            tool = str(call.get("tool") or "")
            result = await _request(
                2 + i,
                {
                    "jsonrpc": "2.0",
                    "id": 2 + i,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": call.get("arguments") or {}},
                },
            )
            content = result.get("content") or []
            text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            results.append(
                {
                    "tool": tool,
                    "text": "\n".join(t for t in text_parts if t),
                    "content": content,
                    "isError": bool(result.get("isError")),
                }
            )
            if result.get("isError"):
                raise RuntimeError(f"stdio MCP tool {tool} reported isError: {results[-1]['text'][:200]}")
            logger.info(f"stdio MCP tools/call ok command={command} tool={tool} chars={len(results[-1]['text'])}")
    finally:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
    return results
