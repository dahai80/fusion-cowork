# 109: fusion-harness kernel shim for hooks.
# Cowork public API (HookManager/HookEvent/HookContext, 14 cowork events,
# priority ordering, fail-closed PRE_* events) lives in hook_manager.py;
# kernel types come from the fusion-harness package ([plugins] extra, #2; 裸装
# 缺包时给出可执行安装指引 — issue #196).
# Cowork hook semantics (priority, HookContext, fail_closed) do not map 1:1
# onto the kernel, so HookManager stays backed by the cowork implementation.

from __future__ import annotations

try:
    from fusion_harness.hooks import (
        HOOKS_CONFIG_PATH,
        HookConfig,
        HookEngine,
        HookResult,
        hook,
    )
except ImportError as exc:
    from ._harness_guard import harness_import_error

    raise harness_import_error(exc) from exc

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
