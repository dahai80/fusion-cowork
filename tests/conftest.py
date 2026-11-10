import os
import tempfile

import pytest

from fusion_cowork import config_center as _cc_mod

# issue #187: HITL 默认 ON — 测试套件整体按 headless 跑 (deny-fast 语义不变),
# HITL 专属测试文件在自己的 fixture 里 monkeypatch.delenv 恢复默认 ON 语义
os.environ.setdefault("FUSION_COWORK_HITL_PAUSE", "0")

# issue #429: CI 按 pytest-split 分片跑 — 分片不再保证含有 import_all_nodes()
# 的测试文件被收集, 节点注册表断言 (72 节点等) 需要套件级完整注册
from fusion_cowork.nodes import import_all_nodes

import_all_nodes()


@pytest.fixture(autouse=True)
def _isolate_config_center():
    tmp = tempfile.mkdtemp(prefix="fc_cfg_")
    cfg_path = os.path.join(tmp, "config.json")
    orig_file = _cc_mod._CONFIG_FILE
    _cc_mod._CONFIG_FILE = cfg_path
    _cc_mod.ConfigCenter.reset_instance()
    yield
    _cc_mod.ConfigCenter.reset_instance()
    _cc_mod._CONFIG_FILE = orig_file
    try:
        os.unlink(cfg_path)
        os.rmdir(tmp)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _isolate_permission_rules(monkeypatch, tmp_path):
    # issue #266: 权限规则持久化在 ~/.fusion-cowork/permissions.json — create_web_app
    # 启动 load() 会读真机规则 (如本机已批准 shell_exec scope=*), 任何依赖权限
    # 拒绝语义的测试都会被真机状态污染。套件级指向临时文件。
    from fusion_cowork.engine import permission_manager as _pm_mod

    monkeypatch.setattr(_pm_mod, "_PERMISSIONS_FILE", str(tmp_path / "permissions.json"))
