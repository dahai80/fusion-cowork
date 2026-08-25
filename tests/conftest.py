import os
import tempfile

import pytest

from fusion_cowork import config_center as _cc_mod


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
