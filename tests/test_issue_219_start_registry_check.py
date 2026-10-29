"""issue #219: start.sh 启动前端口登记自检。

未登记端口必须在 start 即失败 (而不是事后被 CI check-ports 抓到);
FUSION_SKIP_REGISTRY_CHECK=1 显式跳过; 缺登记文件 warn 放行 (独立部署)。
通过 `start.sh registry-check` 子命令直接驱动 registry_selfcheck。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

START_SH = Path(__file__).resolve().parent.parent / "start.sh"

GOOD_REGISTRY = """services:
  fusion-mlx:
    port: 11434
  fusion-cowork:
    port: 11438
    owner: cowork
"""

OTHER_ONLY_REGISTRY = """services:
  fusion-mlx:
    port: 11434
"""

WRONG_PORT_REGISTRY = """services:
  fusion-cowork:
    port: 19999
"""


def _run_registry_check(registry: Path | None, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("FUSION_SKIP_REGISTRY_CHECK", None)
    env["FUSION_REGISTRY"] = str(registry)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(START_SH), "registry-check"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


class TestRegistrySelfcheck:
    def test_registered_port_passes(self, tmp_path):
        reg = tmp_path / "port-registry.yaml"
        reg.write_text(GOOD_REGISTRY)
        result = _run_registry_check(reg)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout

    def test_unregistered_port_fails(self, tmp_path):
        reg = tmp_path / "port-registry.yaml"
        reg.write_text(OTHER_ONLY_REGISTRY)
        result = _run_registry_check(reg)
        assert result.returncode == 1
        assert "refusing to start" in result.stdout

    def test_wrong_port_fails(self, tmp_path):
        reg = tmp_path / "port-registry.yaml"
        reg.write_text(WRONG_PORT_REGISTRY)
        result = _run_registry_check(reg)
        assert result.returncode == 1
        assert "refusing to start" in result.stdout

    def test_skip_env_bypasses(self, tmp_path):
        reg = tmp_path / "port-registry.yaml"
        reg.write_text(OTHER_ONLY_REGISTRY)
        result = _run_registry_check(reg, extra_env={"FUSION_SKIP_REGISTRY_CHECK": "1"})
        assert result.returncode == 0, result.stdout + result.stderr
        assert "bypassed" in result.stdout

    def test_missing_registry_file_warns_and_passes(self, tmp_path):
        result = _run_registry_check(tmp_path / "no-such-file.yaml")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "skipped" in result.stdout

    def test_custom_port_checked(self, tmp_path):
        reg = tmp_path / "port-registry.yaml"
        reg.write_text(GOOD_REGISTRY)
        result = _run_registry_check(reg, extra_env={"FUSION_PORT": "12345"})
        assert result.returncode == 1
        assert "port 12345" in result.stdout
