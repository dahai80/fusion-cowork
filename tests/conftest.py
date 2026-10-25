import os
import tempfile

import pytest

from fusion_cowork import config_center as _cc_mod

# issue #187: HITL 默认 ON — 测试套件整体按 headless 跑 (deny-fast 语义不变),
# HITL 专属测试文件在自己的 fixture 里 monkeypatch.delenv 恢复默认 ON 语义
os.environ.setdefault("FUSION_COWORK_HITL_PAUSE", "0")


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
