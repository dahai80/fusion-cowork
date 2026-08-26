import logging

import pytest

from fusion_cowork.security.quotas import (
    QuotaEnforcer,
    QuotaExceededError,
    TenantQuotas,
    get_default_quota_enforcer,
)


class _FakeConfig:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, key, default=None):
        return self._data.get(key, default)


def test_tenant_quotas_default_unlimited():
    q = TenantQuotas()
    assert q.unlimited is True
    assert q.max_spaces == -1


def test_tenant_quotas_limited():
    q = TenantQuotas(max_spaces=5, max_messages_per_space=100)
    assert q.unlimited is False


def test_no_config_means_unlimited():
    enf = QuotaEnforcer(config=None)
    enf.check_create_space("tA", 9999)
    enf.check_add_message("tA", "sp1", 9999)
    enf.check_create_artifact("tA", "sp1", 9999)
    enf.check_create_agent("tA", "sp1", 9999)
    enf.check_storage("tA", 9999.0)


def test_create_space_over_limit_rejected():
    cfg = _FakeConfig({"quotas.tA.max_spaces": 3})
    enf = QuotaEnforcer(config=cfg)
    enf.check_create_space("tA", 2)
    with pytest.raises(QuotaExceededError):
        enf.check_create_space("tA", 3)


def test_add_message_over_limit_rejected():
    cfg = _FakeConfig({"quotas.tA.max_messages_per_space": 10})
    enf = QuotaEnforcer(config=cfg)
    enf.check_add_message("tA", "sp1", 9)
    with pytest.raises(QuotaExceededError):
        enf.check_add_message("tA", "sp1", 10)


def test_create_artifact_over_limit_rejected():
    cfg = _FakeConfig({"quotas.tA.max_artifacts_per_space": 2})
    enf = QuotaEnforcer(config=cfg)
    enf.check_create_artifact("tA", "sp1", 1)
    with pytest.raises(QuotaExceededError):
        enf.check_create_artifact("tA", "sp1", 2)


def test_create_agent_over_limit_rejected():
    cfg = _FakeConfig({"quotas.tA.max_agents_per_space": 1})
    enf = QuotaEnforcer(config=cfg)
    enf.check_create_agent("tA", "sp1", 0)
    with pytest.raises(QuotaExceededError):
        enf.check_create_agent("tA", "sp1", 1)


def test_storage_over_limit_rejected():
    cfg = _FakeConfig({"quotas.tA.max_storage_mb": 50})
    enf = QuotaEnforcer(config=cfg)
    enf.check_storage("tA", 49.0)
    with pytest.raises(QuotaExceededError):
        enf.check_storage("tA", 50.0)


def test_per_tenant_isolation():
    cfg = _FakeConfig({"quotas.tA.max_spaces": 2})
    enf = QuotaEnforcer(config=cfg)
    with pytest.raises(QuotaExceededError):
        enf.check_create_space("tA", 2)
    enf.check_create_space("tB", 9999)


def test_default_quota_applies_when_tenant_specific_absent():
    cfg = _FakeConfig({"quotas.default.max_spaces": 5})
    enf = QuotaEnforcer(config=cfg)
    with pytest.raises(QuotaExceededError):
        enf.check_create_space("tZ", 5)
    enf.check_create_space("tZ", 4)


def test_tenant_specific_overrides_default():
    cfg = _FakeConfig(
        {
            "quotas.default.max_spaces": 5,
            "quotas.tA.max_spaces": 1,
        }
    )
    enf = QuotaEnforcer(config=cfg)
    with pytest.raises(QuotaExceededError):
        enf.check_create_space("tA", 1)
    enf.check_create_space("tB", 4)
    with pytest.raises(QuotaExceededError):
        enf.check_create_space("tB", 5)


def test_invalid_quota_value_treated_as_unlimited(caplog):
    cfg = _FakeConfig({"quotas.tA.max_spaces": "not-a-number"})
    enf = QuotaEnforcer(config=cfg)
    with caplog.at_level(logging.WARNING):
        enf.check_create_space("tA", 9999)


def test_default_enforcer_singleton_unlimited():
    e1 = get_default_quota_enforcer()
    e2 = get_default_quota_enforcer()
    assert e1 is e2
    e1.check_create_space("any", 999999)
