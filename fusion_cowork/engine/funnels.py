# 111: fusion-harness kernel shim for structural funnels.
# #190/#2: fusion-harness is a hard dependency — no in-tree fallback. An
# import failure is a loud ImportError (fail-visible, not fail-silent).

from __future__ import annotations

from fusion_harness.funnels import (
    EventDomain,
    FunnelOutcome,
    LifecycleBus,
    LifecycleEvent,
    LLMFunnel,
    ToolFunnel,
)

__all__ = [
    "EventDomain",
    "FunnelOutcome",
    "LLMFunnel",
    "LifecycleBus",
    "LifecycleEvent",
    "ToolFunnel",
]
