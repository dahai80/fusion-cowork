"""#109: fusion-harness kernel extraction — cowork shim routing + parity.

Cowork's HookManager/PermissionManager keep cowork-specific semantics
(priority ordering, HookContext.modified_data, fail-closed PRE_*, 5-level
permissions, HIGH_RISK_NODES, confirm_guard) that do not map 1:1 onto the
fusion-harness kernel, so the shims re-export kernel types while the local
copies preserve cowork behavior. The parity checks below mirror
agent-studio's test_issue_357_harness_parity.py where applicable (kernel
source present, fallback import works, public surface, local copy parseable)
without the kernel==local AST drift comparison (cowork API != kernel API).
"""

import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "fusion_cowork" / "engine"

try:
    import fusion_harness  # noqa: F401

    HAVE_KERNEL = True
except ImportError:
    HAVE_KERNEL = False


def _local_ast(mod: str) -> str:
    src = (ENGINE / f"_{mod}_local.py").read_text()
    return ast.dump(ast.parse(src))


def test_kernel_source_present():
    import fusion_cowork.engine.hooks as hooks
    import fusion_cowork.engine.permission as permission

    assert hooks.KERNEL_SOURCE in ("fusion-harness", "local")
    assert permission.KERNEL_SOURCE in ("fusion-harness", "local")


def test_fallback_import_works():
    import importlib

    blocked = {
        "fusion_harness": None,
        "fusion_harness.hooks": None,
        "fusion_harness.permissions": None,
        "fusion_harness.funnels": None,
        "fusion_harness.compactor": None,
    }
    with patch.dict(sys.modules, blocked):
        sys.modules.pop("fusion_cowork.engine.hooks", None)
        sys.modules.pop("fusion_cowork.engine.permission", None)
        hooks = importlib.import_module("fusion_cowork.engine.hooks")
        permission = importlib.import_module("fusion_cowork.engine.permission")
        assert hooks.KERNEL_SOURCE == "local"
        assert permission.KERNEL_SOURCE == "local"
        assert hasattr(hooks, "HookManager")
        assert hasattr(hooks, "HookEvent")
        assert hasattr(hooks, "HookContext")
        assert hasattr(permission, "PermissionManager")
        assert hasattr(permission, "PermissionLevel")
        assert hasattr(permission, "Permission")
        assert hasattr(permission, "HIGH_RISK_NODES")
    sys.modules.pop("fusion_cowork.engine.hooks", None)
    sys.modules.pop("fusion_cowork.engine.permission", None)


@pytest.mark.skipif(not HAVE_KERNEL, reason="fusion-harness not installed")
def test_public_surface_matches_kernel():
    import fusion_cowork.engine.hooks as hooks
    import fusion_cowork.engine.permission as permission

    for mod, names in [
        (hooks, ["HookEngine", "HookConfig", "HookResult", "hook", "HOOKS_CONFIG_PATH"]),
        (permission, ["PermissionEngine", "PermissionRule", "PermissionDecision", "PermissionMode"]),
    ]:
        for name in names:
            assert hasattr(mod, name), f"{mod.__name__} missing {name}"
            assert getattr(mod, name) is not None


def test_local_copy_parseable():
    for mod in ["hooks", "permission"]:
        tree = ast.parse((ENGINE / f"_{mod}_local.py").read_text())
        assert tree.body
