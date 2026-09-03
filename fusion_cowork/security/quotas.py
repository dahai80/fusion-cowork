"""Stage 7 — per-tenant 配额。

TenantQuotas dataclass + QuotaEnforcer: check_create_space/check_add_message/...,
从 ConfigCenter 读配额 (quotas.<tenant_id>.<resource> 或默认), store CRUD 插入前调。

opt-in: 无 config 配额 → 无限 (默认无限, 现有测试零改动)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# 默认配额: -1 = 无限 (opt-in, 不设 → 不限)
DEFAULT_QUOTAS = {
    "max_spaces": -1,
    "max_messages_per_space": -1,
    "max_artifacts_per_space": -1,
    "max_agents_per_space": -1,
    "max_storage_mb": -1,
}


@dataclass
class TenantQuotas:
    max_spaces: int = -1
    max_messages_per_space: int = -1
    max_artifacts_per_space: int = -1
    max_agents_per_space: int = -1
    max_storage_mb: int = -1

    @property
    def unlimited(self) -> bool:
        return all(
            v < 0
            for v in (
                self.max_spaces,
                self.max_messages_per_space,
                self.max_artifacts_per_space,
                self.max_agents_per_space,
                self.max_storage_mb,
            )
        )


class QuotaExceededError(Exception):
    pass


class QuotaEnforcer:
    def __init__(self, config=None, identity_client=None):
        self._config = config
        self._identity_client = identity_client

    def _load_quotas(self, tenant_id: str) -> TenantQuotas:
        # issue #88: identity_client 注入 → 从 cached VerifyResponse.quota 取 (替代 ConfigCenter)
        if self._identity_client is not None:
            return self._load_quotas_from_identity(tenant_id)
        if self._config is None:
            return TenantQuotas()
        try:
            from ..config_center import ConfigCenter

            cc = self._config or ConfigCenter.get_instance()
            q = TenantQuotas()
            for field_name in DEFAULT_QUOTAS:
                # 先查 tenant 专属, 缺则查 default, 缺则 -1
                val = cc.get(f"quotas.{tenant_id}.{field_name}", None)
                if val is None:
                    val = cc.get(f"quotas.default.{field_name}", DEFAULT_QUOTAS[field_name])
                try:
                    setattr(q, field_name, int(val))
                except (TypeError, ValueError):
                    logger.warning(f"配额 {field_name} 值非法 {val!r}, 视为无限")
                    setattr(q, field_name, -1)
            return q
        except Exception as e:
            logger.debug(f"读配额失败, 视为无限: {e}")
            return TenantQuotas()

    def _load_quotas_from_identity(self, tenant_id: str) -> TenantQuotas:
        """issue #88: 从 identity client 的 verify 缓存读 quota dict。

        缓存可能无该 tenant (verify 未发生) → 视为无限 (不阻塞首次访问)。
        """
        q = TenantQuotas()
        try:
            quota = self._identity_client._cache  # dict[jti -> (ts, VerifyResult)]
            for _key, (_ts, result) in getattr(quota, "items", lambda: [])():
                if result is not None and result.tid == tenant_id and result.quota:
                    for field_name in DEFAULT_QUOTAS:
                        val = result.quota.get(field_name)
                        if val is None:
                            continue
                        try:
                            setattr(q, field_name, int(val))
                        except (TypeError, ValueError):
                            logger.warning(f"identity 配额 {field_name} 值非法 {val!r}, 视为无限")
                            setattr(q, field_name, -1)
                    return q
        except Exception as e:
            logger.debug(f"identity 配额读取失败, 视为无限: {e}")
        return q

    def record_usage(
        self,
        tenant_id: str,
        metric: str,
        value: int | float,
        source: str = "fusion-cowork",
        model: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """issue #88: 上报用量到 fusion-identity (best-effort)。"""
        if self._identity_client is None:
            return
        try:
            self._identity_client.emit_usage(tenant_id, metric, value, source=source, model=model, user_id=user_id)
        except Exception as e:
            logger.debug(f"identity usage 上报失败 (best-effort): {e}")

    def check_create_space(self, tenant_id: str, current_count: int) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_spaces < 0:
            return
        if current_count >= q.max_spaces:
            logger.warning(f"配额超限: tenant={tenant_id} spaces {current_count} >= {q.max_spaces}")
            raise QuotaExceededError(f"租户 {tenant_id} 空间数已达上限 {q.max_spaces}")

    def check_add_message(self, tenant_id: str, space_id: str, current_count: int) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_messages_per_space < 0:
            return
        if current_count >= q.max_messages_per_space:
            logger.warning(
                f"配额超限: tenant={tenant_id} space={space_id} 消息 {current_count} >= {q.max_messages_per_space}"
            )
            raise QuotaExceededError(f"空间 {space_id} 消息数已达上限 {q.max_messages_per_space}")

    def check_create_artifact(self, tenant_id: str, space_id: str, current_count: int) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_artifacts_per_space < 0:
            return
        if current_count >= q.max_artifacts_per_space:
            logger.warning(
                f"配额超限: tenant={tenant_id} space={space_id} 产物 {current_count} >= {q.max_artifacts_per_space}"
            )
            raise QuotaExceededError(f"空间 {space_id} 产物数已达上限 {q.max_artifacts_per_space}")

    def check_create_agent(self, tenant_id: str, space_id: str, current_count: int) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_agents_per_space < 0:
            return
        if current_count >= q.max_agents_per_space:
            logger.warning(
                f"配额超限: tenant={tenant_id} space={space_id} agent {current_count} >= {q.max_agents_per_space}"
            )
            raise QuotaExceededError(f"空间 {space_id} agent 数已达上限 {q.max_agents_per_space}")

    def check_storage(self, tenant_id: str, current_mb: float) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_storage_mb < 0:
            return
        if current_mb >= q.max_storage_mb:
            logger.warning(f"配额超限: tenant={tenant_id} 存储 {current_mb} >= {q.max_storage_mb}")
            raise QuotaExceededError(f"租户 {tenant_id} 存储已达上限 {q.max_storage_mb}MB")


_DEFAULT_ENFORCER: Optional[QuotaEnforcer] = None


def get_default_quota_enforcer() -> QuotaEnforcer:
    global _DEFAULT_ENFORCER
    if _DEFAULT_ENFORCER is None:
        _DEFAULT_ENFORCER = QuotaEnforcer()
        logger.debug("QuotaEnforcer 单例构造 (默认无限, config 设 quotas.* 才限)")
    return _DEFAULT_ENFORCER
