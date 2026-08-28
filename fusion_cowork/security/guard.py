"""fusion-guard 集成客户端 (issue #73)。

UDS JSON-RPC over /tmp/fusion-guard.sock, 换行分隔帧。
HIGH_RISK_NODES 委托 guard.evaluate; 低风险不走 guard。
guard 不可达 → 缓存规则 fail-closed (H2)。

纯 cowork 客户端, 不依赖 fusion-guard fg-pyo3 wheel — 直接对 wire contract 编程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_SOCK = "/tmp/fusion-guard.sock"
RULES_CACHE_DIR = os.path.expanduser("~/.fusion-guard")
RULES_CACHE_FILE = os.path.join(RULES_CACHE_DIR, "rules-cache.json")
ENV_ENABLED = "FUSION_GUARD_ENABLED"
ENV_SECRET = "FUSION_GUARD_SHARED_SECRET"
_CONNECT_TIMEOUT = 2.0
_CALL_TIMEOUT = 5.0
_MAX_LINE_BYTES = 1024 * 1024
_FRAMING_NEWLINE = b"\n"


@dataclass
class GuardVerdict:
    action: str
    risk_level: str
    reason: str
    stage: str
    requires_approval: bool
    redacted_content: Optional[str]
    seatbelt_required: bool
    action_id: Optional[str]
    verdict_epoch: int
    verdict_ttl_secs: int
    inferred_category: str
    category_hint: Optional[str]

    @classmethod
    def from_dict(cls, d: dict) -> GuardVerdict:
        return cls(
            action=str(d.get("action", "block")),
            risk_level=str(d.get("risk_level", "l4")),
            reason=str(d.get("reason", "")),
            stage=str(d.get("stage", "")),
            requires_approval=bool(d.get("requires_approval", False)),
            redacted_content=d.get("redacted_content"),
            seatbelt_required=bool(d.get("seatbelt_required", False)),
            action_id=d.get("action_id"),
            verdict_epoch=int(d.get("verdict_epoch", 0)),
            verdict_ttl_secs=int(d.get("verdict_ttl_secs", 0)),
            inferred_category=str(d.get("inferred_category", "")),
            category_hint=d.get("category_hint"),
        )


class GuardClient:
    def __init__(self, sock_path: str = DEFAULT_SOCK, secret: Optional[str] = None, timeout: float = _CALL_TIMEOUT):
        self._sock_path = sock_path
        self._secret = secret or os.environ.get(ENV_SECRET)
        self._timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def _ensure_connected(self) -> bool:
        if self._writer is not None and not self._writer.is_closing():
            return True
        if not os.path.exists(self._sock_path):
            return False
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self._sock_path), timeout=_CONNECT_TIMEOUT
            )
            return True
        except (TimeoutError, OSError) as e:
            logger.warning(f"guard UDS 连接失败 {self._sock_path}: {e}")
            self._writer = None
            self._reader = None
            return False

    async def close(self) -> None:
        w = self._writer
        self._writer = None
        self._reader = None
        if w is not None and not w.is_closing():
            try:
                w.close()
                await w.wait_closed()
            except OSError:
                pass

    async def _call(self, method: str, params: dict) -> Optional[dict]:
        async with self._lock:
            if not await self._ensure_connected():
                return None
            req_id = self._next_id
            self._next_id += 1
            frame_params = dict(params)
            if self._secret and method != "guard.ping":
                frame_params["secret"] = self._secret
            request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": frame_params}
            payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + _FRAMING_NEWLINE
            try:
                self._writer.write(payload)
                await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)
                reply = await asyncio.wait_for(self._read_reply(), timeout=self._timeout)
            except (TimeoutError, OSError, json.JSONDecodeError, asyncio.IncompleteReadError) as e:
                logger.warning(f"guard _call 失败 method={method}: {e}")
                await self.close()
                return None
            if reply is None:
                await self.close()
                return None
            if "error" in reply:
                err = reply["error"]
                logger.warning(
                    "guard error method=%s code=%s msg=%s", method, err.get("code"), err.get("message")
                )
                return None
            return reply.get("result")

    async def _read_reply(self) -> Optional[dict]:
        try:
            raw = await self._reader.readuntil(_FRAMING_NEWLINE)
        except asyncio.IncompleteReadError as e:
            # 上游契约: 响应原始 JSON 无尾换行 → readuntil 抛 IncompleteReadError, 取已读部分
            chunk = e.partial
            if not chunk:
                return None
            return json.loads(chunk.decode("utf-8").strip())
        except asyncio.LimitOverrunError:
            logger.warning("guard 响应超 _MAX_LINE_BYTES, 丢弃")
            await self.close()
            return None
        return json.loads(raw.decode("utf-8").strip())

    async def ping(self) -> bool:
        result = await self._call("guard.ping", {})
        if result is None:
            return False
        return True

    async def evaluate(
        self,
        *,
        content: str,
        caller_epoch: int = 0,
        tenant_id: str = "default",
        requester: str = "unknown",
        action: str = "",
        content_type: str = "shell",
        category_hint: Optional[str] = None,
    ) -> Optional[GuardVerdict]:
        params = {
            "content": content,
            "caller_epoch": caller_epoch,
            "tenant_id": tenant_id,
            "requester": requester,
            "action": action,
            "content_type": content_type,
        }
        if category_hint is not None:
            params["category_hint"] = category_hint
        result = await self._call("guard.evaluate", params)
        if result is None:
            return None
        try:
            return GuardVerdict.from_dict(result)
        except (TypeError, ValueError) as e:
            logger.warning(f"guard evaluate 响应解析失败: {e}")
            return None

    async def confirm(
        self, *, action_id: str, approved: bool = False, approved_by: str = "unknown", tenant_id: str = "default"
    ) -> bool:
        params = {"action_id": action_id, "approved": approved, "approved_by": approved_by, "tenant_id": tenant_id}
        result = await self._call("guard.confirm", params)
        return result is not None

    async def rules_dump(self) -> Optional[Tuple[list, int]]:
        result = await self._call("guard.rules.dump", {})
        if result is None:
            return None
        rules = result.get("rules", [])
        epoch = int(result.get("epoch", 0))
        return rules, epoch


def guard_enabled() -> bool:
    if os.environ.get(ENV_ENABLED) != "1":
        return False
    if not os.path.exists(DEFAULT_SOCK):
        return False
    return True


def load_cached_rules() -> Optional[Tuple[list, int]]:
    try:
        with open(RULES_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        rules = data.get("rules", [])
        epoch = int(data.get("epoch", 0))
        return rules, epoch
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug(f"guard 缓存规则读取失败: {e}")
        return None


def save_cached_rules(rules: list, epoch: int) -> None:
    try:
        os.makedirs(RULES_CACHE_DIR, exist_ok=True)
        payload = json.dumps({"rules": rules, "epoch": epoch}, ensure_ascii=False)
        tmp = RULES_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, RULES_CACHE_FILE)
    except OSError as e:
        logger.warning(f"guard 缓存规则写入失败: {e}")


def get_cached_epoch() -> int:
    cached = load_cached_rules()
    if cached is None:
        return 0
    return cached[1]


_NODE_CONTENT_MAP: dict[str, Tuple[tuple, str]] = {
    "shell_exec": (("command", "shell"), "shell"),
    "python_repl": (("code",), "code"),
    "apply_edit": (("code", "patch", "new_text", "content"), "code"),
    "file_delete": (("path",), "text"),
    "file_copy": (("path", "src", "source"), "text"),
    "file_move": (("path", "src", "source"), "text"),
    "file_find": (("path", "pattern"), "text"),
    "disk_cleaner": (("path", "paths"), "text"),
    "desktop_clean": (("path",), "text"),
    "download_organizer": (("path",), "text"),
    "app_lifecycle": (("app", "bundle_id", "path"), "text"),
    "fetch_url": (("url",), "text"),
    "cdp_evaluate": (("script", "expression"), "code"),
    "cdp_navigate": (("url",), "text"),
    "cdp_click": (("selector", "x", "y"), "text"),
    "cdp_fill": (("selector", "value", "text"), "text"),
    "cdp_fill_form": (("form", "fields"), "json"),
    "cdp_screenshot": (("path",), "text"),
    "cdp_emulate": (("device",), "text"),
    "browser_automate": (("script", "action", "steps"), "code"),
    "keyboard_type": (("text",), "text"),
    "keyboard_shortcut": (("keys", "shortcut"), "text"),
    "mouse_click": (("x", "y"), "text"),
    "mouse_move": (("x", "y"), "text"),
    "screen_capture": (("path",), "text"),
    "clipboard": (("text",), "text"),
    "notification": (("title", "body", "message"), "text"),
    "computer_use_loop": (("goal", "prompt"), "text"),
}


def node_to_guard_content(tool_name: str, params: Optional[dict] = None) -> Tuple[str, str]:
    params = params or {}
    entry = _NODE_CONTENT_MAP.get(tool_name)
    if entry is not None:
        keys, content_type = entry
        for key in keys:
            val = params.get(key)
            if val:
                return str(val), content_type
        return tool_name, content_type
    fallback = f"{tool_name}:{json.dumps(params, ensure_ascii=False, sort_keys=True)}"
    return fallback, "json"


_guard_client: Optional[GuardClient] = None


def get_guard_client() -> Optional[GuardClient]:
    global _guard_client
    if not guard_enabled():
        return None
    if _guard_client is None:
        _guard_client = GuardClient()
        logger.info("guard 集成已启用, UDS=%s", DEFAULT_SOCK)
    return _guard_client


async def close_guard_client() -> None:
    global _guard_client
    if _guard_client is not None:
        await _guard_client.close()
        _guard_client = None
