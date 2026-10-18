# 109: fusion-harness kernel shim for hooks.
# Canonical kernel lives in fusion-harness (pip install fusion-harness).
# In-tree copy kept as _hooks_local fallback so CI (no harness extra) stays green.
# Cowork public API (HookManager/HookEvent/HookContext, 14 cowork events,
# priority ordering, fail-closed PRE_* events) is preserved by the local copy;
# kernel types (HookEngine/HookConfig/HookResult/hook/HOOKS_CONFIG_PATH) are
# re-exported when the harness is installed and KERNEL_SOURCE tracks the active
# source. Cowork hook semantics (priority, HookContext, fail_closed) do not map
# 1:1 onto the kernel, so HookManager stays backed by the local implementation.

from __future__ import annotations

try:
    from fusion_harness.hooks import (
        HOOKS_CONFIG_PATH,
        HookConfig,
        HookEngine,
        HookResult,
        hook,
    )

    KERNEL_SOURCE = "fusion-harness"
except ImportError:  # pragma: no cover - fallback when harness not installed
    KERNEL_SOURCE = "local"

from ._hooks_local import (
    HookContext,
    HookEvent,
    HookManager,
)

__all__ = [
    "HOOKS_CONFIG_PATH",
    "HookConfig",
    "HookEngine",
    "HookEvent",
    "HookContext",
    "HookManager",
    "HookResult",
    "KERNEL_SOURCE",
    "hook",
]
