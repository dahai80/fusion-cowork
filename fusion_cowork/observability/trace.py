"""统一 trace_id 上下文 (v0.4.0 Stage 4).

contextvar 持 trace_id, 跨 await 自动传播 (替散落 secrets.token_hex(8) / mcp_<hex> 格式)。
格式: fc_<16hex>。缺则自动生并注入。

用法:
    from fusion_cowork.observability.trace import get_trace_id
    tid = get_trace_id()  # 首次调用自动生
    async with trace_context(tid):
        ...  # 块内 get_trace_id() 返 tid
"""

from __future__ import annotations

import contextvars
import logging
import secrets
from typing import Optional

logger = logging.getLogger(__name__)

_TRACE_PREFIX = "fc"
_TRACE_HEX_LEN = 16

_trace_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("fusion_trace_id", default=None)


def new_trace_id() -> str:
    return f"{_TRACE_PREFIX}_{secrets.token_hex(_TRACE_HEX_LEN // 2)}"


def get_trace_id() -> str:
    tid = _trace_var.get()
    if not tid:
        tid = new_trace_id()
        _trace_var.set(tid)
    return tid


def set_trace_id(tid: Optional[str]) -> contextvars.Token:
    return _trace_var.set(tid)


class trace_context:
    """async ctx mgr — 块内 get_trace_id() 返指定 tid; 退出恢复。"""

    def __init__(self, tid: Optional[str] = None):
        self._tid = tid or new_trace_id()
        self._token: Optional[contextvars.Token] = None

    async def __aenter__(self) -> str:
        self._token = _trace_var.set(self._tid)
        return self._tid

    async def __aexit__(self, *exc) -> None:
        if self._token is not None:
            _trace_var.reset(self._token)
