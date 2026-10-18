# 109: fusion-harness kernel shim for permissions.
# Canonical kernel lives in fusion-harness (pip install fusion-harness).
# In-tree copy kept as _permission_local fallback so CI (no harness extra) stays green.
# Cowork public API (PermissionManager/PermissionLevel/Permission/HIGH_RISK_NODES/
# confirm_guard/list_pending_guard/guard delegation, 5 cowork levels) is
# preserved by the local copy; kernel types
# (PermissionEngine/PermissionRule/PermissionDecision/PermissionMode) are
# re-exported when the harness is installed and KERNEL_SOURCE tracks the active
# source. Cowork check() semantics (deny-rules-first, HIGH_RISK_NODES guard
# delegation, confirm_guard, atomic save/load) do not map 1:1 onto the kernel,
# so PermissionManager stays backed by the local implementation.

from __future__ import annotations

try:
    from fusion_harness.permissions import (
        PermissionDecision,
        PermissionEngine,
        PermissionMode,
        PermissionRule,
    )

    KERNEL_SOURCE = "fusion-harness"
except ImportError:  # pragma: no cover - fallback when harness not installed
    KERNEL_SOURCE = "local"

from ._permission_local import (
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
    "KERNEL_SOURCE",
]
