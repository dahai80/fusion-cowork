# 109: fusion-harness kernel shim for permissions.
# Cowork public API (PermissionManager/PermissionLevel/Permission/
# HIGH_RISK_NODES/confirm_guard/list_pending_guard/guard delegation, 5
# cowork levels) lives in permission_manager.py; kernel types come from the
# fusion-harness package ([plugins] extra, #2; a bare install gets an
# actionable install hint — issue #196). issue #493 (K1): rule-model/scope-matching semantics now
# delegate to fusion_harness.scoped_permissions; PermissionManager keeps the
# cowork check() orchestration (hook audit, HITL gate exemption, trusted-sync,
# capability gate, guard IPC) — kernel scoped_permissions docstring records
# those as consumer wiring.

from __future__ import annotations

try:
    from fusion_harness.permissions import (
        PermissionDecision,
        PermissionEngine,
        PermissionMode,
        PermissionRule,
    )
    from fusion_harness.scoped_permissions import (
        DECISIONS as KERNEL_DECISIONS,
    )
    from fusion_harness.scoped_permissions import (
        PendingApproval as KernelPendingApproval,
    )
    from fusion_harness.scoped_permissions import (
        ScopedPermission as KernelScopedPermission,
    )
    from fusion_harness.scoped_permissions import (
        ScopedPermissionEngine as KernelScopedPermissionEngine,
    )
    from fusion_harness.scoped_permissions import (
        ScopedPermissionError as KernelScopedPermissionError,
    )
except ImportError as exc:
    from ._harness_guard import harness_import_error

    raise harness_import_error(exc) from exc

from .permission_manager import (
    HIGH_RISK_NODES,
    Permission,
    PermissionLevel,
    PermissionManager,
    _guard_cached_epoch,
)

__all__ = [
    "HIGH_RISK_NODES",
    "KERNEL_DECISIONS",
    "KernelPendingApproval",
    "KernelScopedPermission",
    "KernelScopedPermissionEngine",
    "KernelScopedPermissionError",
    "Permission",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionLevel",
    "PermissionManager",
    "PermissionMode",
    "PermissionRule",
    "_guard_cached_epoch",
]
