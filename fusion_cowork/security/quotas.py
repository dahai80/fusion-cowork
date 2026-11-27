"""Stage 7 — per-tenant quotas.

TenantQuotas dataclass + QuotaEnforcer: check_create_space/check_add_message/...,
reads quotas from ConfigCenter (quotas.<tenant_id>.<resource> or default), called before store CRUD inserts.

opt-in: no config quotas → unlimited (default unlimited, zero change to existing tests).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Default quotas: -1 = unlimited (opt-in, unset → unlimited)
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
        # issue #88: identity_client injected → read from cached VerifyResponse.quota (replacing ConfigCenter)
        if self._identity_client is not None:
            return self._load_quotas_from_identity(tenant_id)
        if self._config is None:
            return TenantQuotas()
        try:
            from ..config_center import ConfigCenter

            cc = self._config or ConfigCenter.get_instance()
            q = TenantQuotas()
            for field_name in DEFAULT_QUOTAS:
                # Check tenant-specific first, then default, then -1
                val = cc.get(f"quotas.{tenant_id}.{field_name}", None)
                if val is None:
                    val = cc.get(f"quotas.default.{field_name}", DEFAULT_QUOTAS[field_name])
                try:
                    setattr(q, field_name, int(val))
                except (TypeError, ValueError):
                    logger.warning(f"Quota {field_name} invalid value {val!r}, treating as unlimited")
                    setattr(q, field_name, -1)
            return q
        except Exception as e:
            logger.debug(f"Failed to read quotas, treating as unlimited: {e}")
            return TenantQuotas()

    def _load_quotas_from_identity(self, tenant_id: str) -> TenantQuotas:
        """issue #88: read the quota dict from the identity client's verify cache.

        The cache may not contain the tenant (verify never happened) → treat as unlimited
        (do not block first access).
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
                            logger.warning(f"Identity quota {field_name} invalid value {val!r}, treating as unlimited")
                            setattr(q, field_name, -1)
                    return q
        except Exception as e:
            logger.debug(f"Failed to read identity quotas, treating as unlimited: {e}")
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
        """issue #88: report usage to fusion-identity (best-effort)."""
        if self._identity_client is None:
            return
        try:
            self._identity_client.emit_usage(tenant_id, metric, value, source=source, model=model, user_id=user_id)
        except Exception as e:
            logger.debug(f"Identity usage report failed (best-effort): {e}")

    def check_create_space(self, tenant_id: str, current_count: int) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_spaces < 0:
            return
        if current_count >= q.max_spaces:
            logger.warning(f"Quota exceeded: tenant={tenant_id} spaces {current_count} >= {q.max_spaces}")
            raise QuotaExceededError(f"租户 {tenant_id} 空间数已达上限 {q.max_spaces}")  # i18n: user-facing

    def check_add_message(self, tenant_id: str, space_id: str, current_count: int) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_messages_per_space < 0:
            return
        if current_count >= q.max_messages_per_space:
            logger.warning(
                f"Quota exceeded: tenant={tenant_id} space={space_id} messages {current_count} >= {q.max_messages_per_space}"
            )
            raise QuotaExceededError(f"空间 {space_id} 消息数已达上限 {q.max_messages_per_space}")  # i18n: user-facing

    def check_create_artifact(self, tenant_id: str, space_id: str, current_count: int) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_artifacts_per_space < 0:
            return
        if current_count >= q.max_artifacts_per_space:
            logger.warning(
                f"Quota exceeded: tenant={tenant_id} space={space_id} artifacts {current_count} >= {q.max_artifacts_per_space}"
            )
            raise QuotaExceededError(f"空间 {space_id} 产物数已达上限 {q.max_artifacts_per_space}")  # i18n: user-facing

    def check_create_agent(self, tenant_id: str, space_id: str, current_count: int) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_agents_per_space < 0:
            return
        if current_count >= q.max_agents_per_space:
            logger.warning(
                f"Quota exceeded: tenant={tenant_id} space={space_id} agents {current_count} >= {q.max_agents_per_space}"
            )
            raise QuotaExceededError(f"空间 {space_id} agent 数已达上限 {q.max_agents_per_space}")  # i18n: user-facing

    def check_storage(self, tenant_id: str, current_mb: float) -> None:
        q = self._load_quotas(tenant_id)
        if q.max_storage_mb < 0:
            return
        if current_mb >= q.max_storage_mb:
            logger.warning(f"Quota exceeded: tenant={tenant_id} storage {current_mb} >= {q.max_storage_mb}")
            raise QuotaExceededError(f"租户 {tenant_id} 存储已达上限 {q.max_storage_mb}MB")  # i18n: user-facing


_DEFAULT_ENFORCER: Optional[QuotaEnforcer] = None


def get_default_quota_enforcer() -> QuotaEnforcer:
    global _DEFAULT_ENFORCER
    if _DEFAULT_ENFORCER is None:
        _DEFAULT_ENFORCER = QuotaEnforcer()
        logger.debug(
            "QuotaEnforcer singleton constructed (unlimited by default, limits apply only when config sets quotas.*)"
        )
    return _DEFAULT_ENFORCER
