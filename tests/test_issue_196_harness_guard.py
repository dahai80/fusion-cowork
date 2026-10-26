"""#196: harness 缺包时引擎 shim 给出可执行安装指引, 不再裸抛 ImportError。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from fusion_cowork.engine._harness_guard import HARNESS_INSTALL_HINT

# 4 个引擎 shim 模块在 import 期 unconditionally 取 fusion_harness 符号
SHIM_MODULES = [
    "fusion_cowork.engine.hooks",
    "fusion_cowork.engine.funnels",
    "fusion_cowork.engine.compactor",
    "fusion_cowork.engine.permission",
]


def _make_blocker(tmp_path: Path) -> Path:
    # 假 fusion_harness 包: import 即抛 ImportError, 模拟裸装缺包
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
        # 报错必须带可执行安装指引, 原始异常链也保留
        assert HARNESS_INSTALL_HINT in proc.stderr
        assert "fusion-harness kernel package is missing" in proc.stderr
        assert "blocked for test" in proc.stderr

    def test_hint_mentions_both_install_routes(self):
        assert "fusion-cowork[plugins]" in HARNESS_INSTALL_HINT
        assert "git+https://github.com/dahai80/fusion-harness" in HARNESS_INSTALL_HINT

    def test_harness_present_imports_clean(self):
        # 工作区 venv 装有 fusion-harness — 4 个 shim 正常导入, guard 不触发
        for module in SHIM_MODULES:
            __import__(module)
