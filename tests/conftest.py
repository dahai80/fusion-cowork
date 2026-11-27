import os
import tempfile

import pytest

from fusion_cowork import config_center as _cc_mod

# issue #187: HITL defaults ON — the suite runs headless overall (deny-fast semantics unchanged);
# HITL-specific test files restore default-ON semantics via monkeypatch.delenv in their own fixtures
os.environ.setdefault("FUSION_COWORK_HITL_PAUSE", "0")

# issue #527: the engine HITL queue uses the kernel HumanGate (persistent JSON queue) — point the
# suite at a temp dir instead of reading/writing the real ~/.fusion-cowork/hitl_gate
os.environ.setdefault("FUSION_COWORK_HITL_GATE_DIR", tempfile.mkdtemp(prefix="fc_hitl_gate_"))

# issue #429: CI runs shards via pytest-split — a shard no longer guarantees collecting the test
# files that call import_all_nodes(), so node-registry assertions (72 nodes etc.) need suite-level full registration
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
    # issue #266: permission rules persist in ~/.fusion-cowork/permissions.json — create_web_app
    # calls load() at startup and would read real machine rules (e.g. an already-approved
    # shell_exec scope=*), polluting any test that depends on deny semantics. Point the suite at a temp file.
    from fusion_cowork.engine import permission_manager as _pm_mod

    monkeypatch.setattr(_pm_mod, "_PERMISSIONS_FILE", str(tmp_path / "permissions.json"))


@pytest.fixture(autouse=True)
def _isolate_web_state_dir(monkeypatch, tmp_path):
    # Durable HITL (#463) recovery reads ApprovalJournal + RunStateStore from
    # ~/.fusion-cowork/web at app startup — a real machine's stale crashed runs
    # restore pending approvals into every test client (observed: 35 leaked
    # approvals). Redirect the web state dir per test via the env override
    # both stores honor.
    monkeypatch.setenv("FUSION_COWORK_WEB_DIR", str(tmp_path / "webstate"))
