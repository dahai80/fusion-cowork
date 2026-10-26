# 111: fusion-harness kernel shim for structural funnels.
# #190/#2: fusion-harness is required at import — no in-tree fallback. 裸装
# 缺包时给出可执行安装指引 (issue #196, fail-visible, not fail-silent).

from __future__ import annotations

try:
    from fusion_harness.funnels import (
        EventDomain,
        FunnelOutcome,
        LifecycleBus,
        LifecycleEvent,
        LLMFunnel,
        ToolFunnel,
    )
except ImportError as exc:
    from ._harness_guard import harness_import_error

    raise harness_import_error(exc) from exc

__all__ = [
    "EventDomain",
    "FunnelOutcome",
    "LLMFunnel",
    "LifecycleBus",
    "LifecycleEvent",
    "ToolFunnel",
]
