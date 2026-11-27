"""Unified trace_id context (v0.4.0 Stage 4).

A contextvar holds the trace_id, propagated automatically across await (replacing the scattered secrets.token_hex(8) / mcp_<hex> formats).
Format: fc_<16hex>. Auto-generated and injected when missing.

Usage:
    from fusion_cowork.observability.trace import get_trace_id
    tid = get_trace_id()  # auto-generated on first call
    async with trace_context(tid):
        ...  # get_trace_id() returns tid inside the block
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


def trace_propagation_headers() -> dict:
    # issue #464: OTel spine — outbound LLM calls carry trace context:
    # x-fusion-trace-id (in-platform correlation) + W3C traceparent (gateway/upstream passthrough, format pinned before upstream support)
    tid = _trace_var.get()
    if not tid:
        return {}
    hex_chars = "".join(c for c in tid if c in "0123456789abcdefABCDEF")
    hex32 = hex_chars.ljust(32, "0")[:32]
    return {
        "x-fusion-trace-id": tid,
        "traceparent": f"00-{hex32}-{hex32[:16]}-01",
    }


class trace_context:
    """async ctx mgr — get_trace_id() returns the given tid inside the block; restored on exit."""

    def __init__(self, tid: Optional[str] = None):
        self._tid = tid or new_trace_id()
        self._token: Optional[contextvars.Token] = None

    async def __aenter__(self) -> str:
        self._token = _trace_var.set(self._tid)
        return self._tid

    async def __aexit__(self, *exc) -> None:
        if self._token is not None:
            _trace_var.reset(self._token)
