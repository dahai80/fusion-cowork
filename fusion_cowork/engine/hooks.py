# 109: fusion-harness kernel shim for hooks.
# Cowork public API (HookManager/HookEvent/HookContext, 14 cowork events,
# priority ordering, fail-closed PRE_* events) lives in hook_manager.py;
# kernel types come from the fusion-harness package (hard dependency, #2).
# Cowork hook semantics (priority, HookContext, fail_closed) do not map 1:1
# onto the kernel, so HookManager stays backed by the cowork implementation.

from __future__ import annotations

from fusion_harness.hooks import (
    HOOKS_CONFIG_PATH,
    HookConfig,
    HookEngine,
    HookResult,
    hook,
)

from .hook_manager import (
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
    "hook",
]
