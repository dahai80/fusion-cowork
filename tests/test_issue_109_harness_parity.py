"""#109/#190/#2: fusion-harness kernel — hard dependency + cowork semantics.

Cowork's HookManager/PermissionManager keep cowork-specific semantics
(priority ordering, HookContext.modified_data, fail-closed PRE_*, 5-level
permissions, HIGH_RISK_NODES, confirm_guard) that do not map 1:1 onto the
fusion-harness kernel: the shims re-export kernel types from the package
(hard dependency — no fallback copies), while hook_manager.py /
permission_manager.py carry the cowork behavior. The behavioral contract
both sides reproduce lives in fusion-harness docs/contract.md.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "fusion_cowork" / "engine"


def test_hard_dependency_import_error_is_loud():
    code = (
        "import sys\n"
        "for m in ('fusion_harness', 'fusion_harness.hooks', "
        "'fusion_harness.permissions'):\n"
        "    sys.modules[m] = None\n"
        "try:\n"
        "    import fusion_cowork.engine.hooks\n"
        "except ImportError:\n"
        "    print('LOUD_IMPORT_ERROR')\n"
        "    sys.exit(0)\n"
        "print('UNEXPECTED_SUCCESS')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "LOUD_IMPORT_ERROR" in out.stdout, out.stdout + out.stderr


def test_no_kernel_source_shims_remain():
    # #2 AC1: zero hits for KERNEL_SOURCE / _funnels_local in cowork.
    import fusion_cowork.engine.hooks as hooks
    import fusion_cowork.engine.permission as permission

    assert not hasattr(hooks, "KERNEL_SOURCE")
    assert not hasattr(permission, "KERNEL_SOURCE")


def test_public_surface_matches_kernel():
    import fusion_cowork.engine.hooks as hooks
    import fusion_cowork.engine.permission as permission

    for mod, names in [
        (hooks, ["HOOKS_CONFIG_PATH", "HookConfig", "HookEngine", "HookResult", "hook"]),
        (permission, ["PermissionDecision", "PermissionEngine", "PermissionMode", "PermissionRule"]),
    ]:
        for name in names:
            assert hasattr(mod, name), f"{mod.__name__} missing {name}"
            assert getattr(mod, name) is not None


def test_cowork_manager_surface_preserved():
    import fusion_cowork.engine.hooks as hooks
    import fusion_cowork.engine.permission as permission

    for name in ["HookManager", "HookEvent", "HookContext"]:
        assert hasattr(hooks, name), f"hooks missing {name}"
    for name in ["PermissionManager", "PermissionLevel", "Permission", "HIGH_RISK_NODES"]:
        assert hasattr(permission, name), f"permission missing {name}"


def test_no_fallback_copies_remain():
    leftovers = sorted(p.name for p in ENGINE.glob("_*_local.py"))
    assert leftovers == [], f"kernel fallback copies still present: {leftovers}"


@pytest.mark.parametrize("path", ["hook_manager.py", "permission_manager.py"])
def test_manager_modules_parseable(path):
    import ast

    tree = ast.parse((ENGINE / path).read_text())
    assert tree.body
