"""#196: when the harness package is missing, engine shims give an actionable install hint instead of a bare ImportError."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from fusion_cowork.engine._harness_guard import HARNESS_INSTALL_HINT

# The 4 engine shim modules unconditionally pull fusion_harness symbols at import time
SHIM_MODULES = [
    "fusion_cowork.engine.hooks",
    "fusion_cowork.engine.funnels",
    "fusion_cowork.engine.compactor",
    "fusion_cowork.engine.permission",
    # issue #521: trajectory schema hard-imports the kernel too (drop-fallback
    # ruling) — no soft-degradation mode may reintroduce a local schema
    "fusion_cowork.trajectory.recorder",
    # issue #521: the engine package itself asserts availability at import
    "fusion_cowork.engine",
]


def _make_blocker(tmp_path: Path) -> Path:
    # Fake fusion_harness package: raises ImportError on import, simulating a bare install with the package missing
    pkg = tmp_path / "fusion_harness"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('raise ImportError("blocked for test")\n')
    return tmp_path


def _run_import(module: str, pythonpath: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


class TestHarnessGuard:
    @pytest.mark.parametrize("module", SHIM_MODULES)
    def test_missing_harness_loud_message(self, module, tmp_path):
        blocker = _make_blocker(tmp_path)
        proc = _run_import(module, str(blocker))
        assert proc.returncode != 0
        # The error must carry the actionable install hint, and the original exception chain is preserved
        assert HARNESS_INSTALL_HINT in proc.stderr
        assert "fusion-harness kernel package is missing" in proc.stderr
        assert "blocked for test" in proc.stderr

    def test_hint_mentions_both_install_routes(self):
        assert "fusion-cowork[plugins]" in HARNESS_INSTALL_HINT
        assert "git+https://github.com/dahai80/fusion-harness" in HARNESS_INSTALL_HINT

    def test_harness_present_imports_clean(self):
        # The workspace venv has fusion-harness installed — the 4 shims import normally, guard does not trigger
        for module in SHIM_MODULES:
            __import__(module)
