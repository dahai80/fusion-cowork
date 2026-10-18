# 111: fusion-harness kernel shim for structural funnels.
# fusion_harness.funnels imports fusion_harness.permissions, so the in-tree
# fallback ships its own kernel-permissions copy (_kernel_permissions_local.py)
# and stays importable when the harness is absent (CI path).

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

    KERNEL_SOURCE = "fusion-harness"
except ImportError:
    from ._funnels_local import (
        EventDomain,
        FunnelOutcome,
        LifecycleBus,
        LifecycleEvent,
        LLMFunnel,
        ToolFunnel,
    )

    KERNEL_SOURCE = "local"

__all__ = [
    "KERNEL_SOURCE",
    "EventDomain",
    "FunnelOutcome",
    "LLMFunnel",
    "LifecycleBus",
    "LifecycleEvent",
    "ToolFunnel",
]
