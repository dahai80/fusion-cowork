"""LSP 代码智能客户端 — P2-3。

通用 Language Server Protocol (LSP 3.17) 客户端, JSON-RPC over stdio。
支持 definition / references / hover / completion。自动探测可用 server:
Python: pylsp > pyright > pyrefly; 未安装则降级报错。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LSP_PROTOCOL_VERSION = "3.17"


def _file_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _uri_to_path(uri: str) -> str:
    if uri.startswith("file://"):
        return str(Path(uri[7:]))
    return uri


class LSPClient:
    """LSP 客户端 — 与 language server 经 stdio 通信。"""

    def __init__(self, command: List[str], root_uri: str, workspace_folders: Optional[List[str]] = None):
        self.command = command
        self.root_uri = root_uri
        self.workspace_folders = workspace_folders or []
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._seq = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._buffer = b""

    @classmethod
    def for_python(cls, root_path: str) -> LSPClient:
        root_uri = _file_uri(root_path)
        cmd = cls._detect_python_server()
        return cls(command=cmd, root_uri=root_uri)

    @staticmethod
    def _detect_python_server() -> List[str]:
        for name, args in (
            ("pylsp", []),
            ("pyright-langserver", ["--stdio"]),
            ("pyrefly", ["lsp"]),
        ):
            exe = _which(name)
            if exe:
                logger.info(f"LSP 探测到 server: {name} ({exe})")
                return [exe, *args]
        raise RuntimeError("无可用 Python LSP server, 请安装 pylsp/pyright/pyrefly")

    async def start(self) -> None:
        env = os.environ.copy()
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._initialize()
        logger.info(f"LSPClient 已启动: {self.command}")

    async def _reader_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                self._buffer += chunk
                self._consume_buffer()
        except Exception as e:
            logger.error(f"LSP reader 异常: {e}")

    def _consume_buffer(self) -> None:
        while b"\r\n\r\n" in self._buffer:
            header, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
            length = self._parse_content_length(header)
            if length is None or len(self._buffer) < length:
                return
            body = self._buffer[:length]
            self._buffer = self._buffer[length:]
            try:
                msg = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            self._dispatch(msg)

    @staticmethod
    def _parse_content_length(header: bytes) -> Optional[int]:
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    return int(line.split(b":", 1)[1].strip())
                except ValueError:
                    return None
        return None

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        if "id" in msg and msg["id"] in self._pending:
            fut = self._pending.pop(msg["id"])
            if not fut.done():
                fut.set_result(msg.get("result"))
        elif "method" in msg:
            logger.debug(f"LSP notification: {msg.get('method')}")

    async def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self._seq += 1
        req_id = self._seq
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[req_id] = fut
        body = json.dumps(req).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(header + body)
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=30)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"LSP 请求超时: {method}") from None

    async def _send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(header + body)
        await self._proc.stdin.drain()

    async def _initialize(self) -> Any:
        result = await self._send_request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.root_uri,
                "capabilities": {
                    "textDocument": {
                        "definition": {"linkSupport": False},
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "references": {},
                    }
                },
            },
        )
        await self._send_notification("initialized", {})
        self._initialized = True
        return result

    async def open_document(self, path: str, text: str = "") -> None:
        if not text:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        await self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": _file_uri(path),
                    "languageId": _language_id(path),
                    "version": 1,
                    "text": text,
                }
            },
        )

    async def definition(self, path: str, line: int, character: int) -> List[Dict[str, Any]]:
        result = await self._send_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": _file_uri(path)},
                "position": {"line": line, "character": character},
            },
        )
        return _normalize_locations(result)

    async def references(self, path: str, line: int, character: int) -> List[Dict[str, Any]]:
        result = await self._send_request(
            "textDocument/references",
            {
                "textDocument": {"uri": _file_uri(path)},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": True},
            },
        )
        return _normalize_locations(result)

    async def hover(self, path: str, line: int, character: int) -> Optional[Dict[str, Any]]:
        result = await self._send_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": _file_uri(path)},
                "position": {"line": line, "character": character},
            },
        )
        if not result:
            return None
        return {
            "contents": _hover_contents(result.get("contents")),
            "range": result.get("range"),
        }

    async def completion(self, path: str, line: int, character: int) -> List[Dict[str, Any]]:
        result = await self._send_request(
            "textDocument/completion",
            {
                "textDocument": {"uri": _file_uri(path)},
                "position": {"line": line, "character": character},
            },
        )
        if not result:
            return []
        items = result.get("items", result) if isinstance(result, dict) else result
        return items or []

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc:
            try:
                await self._send_request("shutdown", {})
                await self._send_notification("exit", {})
            except Exception:
                pass
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._initialized = False
        logger.info("LSPClient 已停止")


def _which(name: str) -> Optional[str]:
    import shutil

    return shutil.which(name)


def _language_id(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".py": "python", ".js": "javascript", ".ts": "typescript", ".rs": "rust", ".go": "go"}.get(ext, "plaintext")


def _normalize_locations(result: Any) -> List[Dict[str, Any]]:
    if not result:
        return []
    if isinstance(result, dict):
        result = [result]
    out = []
    for loc in result:
        uri = loc.get("uri", "")
        rng = loc.get("range", {})
        start = rng.get("start", {})
        out.append(
            {
                "path": _uri_to_path(uri),
                "line": start.get("line", 0),
                "character": start.get("character", 0),
                "end_line": rng.get("end", {}).get("line", start.get("line", 0)),
                "end_character": rng.get("end", {}).get("character", start.get("character", 0)),
            }
        )
    return out


def _hover_contents(contents: Any) -> str:
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return contents.get("value", "")
    if isinstance(contents, list):
        parts = []
        for c in contents:
            if isinstance(c, dict):
                parts.append(c.get("value", ""))
            else:
                parts.append(str(c))
        return "\n".join(parts)
    return str(contents)


async def query(
    action: str,
    path: str,
    line: int,
    character: int,
    root: str = "",
) -> Dict[str, Any]:
    """便捷入口: 启动 LSP → 打开文档 → 查询 → 停止。"""
    root = root or str(Path(path).resolve().parent)
    try:
        client = LSPClient.for_python(root)
    except RuntimeError as e:
        return {"error": str(e)}
    try:
        await client.start()
        await client.open_document(path)
        if action == "definition":
            return {"definition": await client.definition(path, line, character)}
        elif action == "references":
            return {"references": await client.references(path, line, character)}
        elif action == "hover":
            return {"hover": await client.hover(path, line, character)}
        elif action == "completion":
            return {"completion": await client.completion(path, line, character)}
        return {"error": f"未知 action: {action}"}
    except Exception as e:
        logger.error(f"LSP query 失败: {e}")
        return {"error": str(e)}
    finally:
        await client.stop()
