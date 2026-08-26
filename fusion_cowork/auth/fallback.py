"""静态 token fallback (v0.4.0 Stage 2) — 本地/桌面保留, 生产强制 JWT。

- verify_static_token(token, expected): 等值比对 (常量时间), 返 TenantPrincipal (默认租户/local_user)
- require_jwt(): env FUSION_REQUIRE_JWT=1 → 生产模式, 静态 fallback 被禁
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fusion_cowork.tenant import DEFAULT_TENANT, LOCAL_USER, TenantPrincipal

logger = logging.getLogger(__name__)


def require_jwt() -> bool:
    """生产模式开关: env FUSION_REQUIRE_JWT=1 → 强制 JWT, 禁静态 fallback。"""
    return os.environ.get("FUSION_REQUIRE_JWT", "") == "1"


def verify_static_token(token: Optional[str], expected: Optional[str]) -> Optional[TenantPrincipal]:
    """静态 token 校验 (本地/桌面)。

    配了 expected → token 须常量时间等值, 通过返默认 principal (local_user/default tenant);
    未配 expected (本地无认证) → 无条件返默认 principal (向后兼容单用户);
    require_jwt()=1 且 expected 未配 → 返 None (生产强制 JWT, 无静态降级)。
    失败返 None + 日志 (不泄 token 原文)。
    """
    if require_jwt() and not expected:
        logger.warning("生产模式 FUSION_REQUIRE_JWT=1 且无静态 token, 拒绝降级认证")
        return None
    if not expected:
        return TenantPrincipal(tenant_id=DEFAULT_TENANT, user_id=LOCAL_USER)
    if not token or not isinstance(token, str):
        logger.warning("静态 token 校验失败: token 缺失")
        return None
    if not hmac.compare_digest(token, expected):
        logger.warning("静态 token 校验失败: token 不匹配")
        return None
    return TenantPrincipal(tenant_id=DEFAULT_TENANT, user_id=LOCAL_USER)


def verify_any_token(token: Optional[str], expected: Optional[str]) -> Optional[TenantPrincipal]:
    """双模校验: JWT 优先, 静态 token fallback。WS/sync/remote 共用。

    - JWT verifier active + token 是有效 JWT → 返 JWT claim principal
    - 否则静态 token 校验 (verify_static_token)
    - require_jwt()=1 + 无 JWT 通过 + 无静态 expected → None (生产强制)
    """
    if token and isinstance(token, str):
        try:
            from fusion_cowork.auth.jwt import get_default_verifier

            verifier = get_default_verifier()
            if verifier.active:
                jwt_principal = verifier.verify_token(token)
                if jwt_principal is not None:
                    return jwt_principal
        except Exception as e:
            logger.warning(f"JWT 校验异常, 回退静态 token: {e}")
    return verify_static_token(token, expected)
