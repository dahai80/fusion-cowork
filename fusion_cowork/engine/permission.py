# 109: fusion-harness kernel shim for permissions.
# Cowork public API (PermissionManager/PermissionLevel/Permission/
# HIGH_RISK_NODES/confirm_guard/list_pending_guard/guard delegation, 5
# cowork levels) lives in permission_manager.py; kernel types come from the
# fusion-harness package (hard dependency, #2). Cowork check() semantics
# (deny-rules-first, HIGH_RISK_NODES guard delegation, confirm_guard,
# atomic save/load) do not map 1:1 onto the kernel, so PermissionManager
# stays backed by the cowork implementation.

from __future__ import annotations

from fusion_harness.permissions import (
    PermissionDecision,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)

from .permission_manager import (
    HIGH_RISK_NODES,
    Permission,
    PermissionLevel,
    PermissionManager,
    _guard_cached_epoch,
)

__all__ = [
    "HIGH_RISK_NODES",
    "Permission",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionLevel",
    "PermissionManager",
    "PermissionMode",
    "PermissionRule",
    "_guard_cached_epoch",
]
