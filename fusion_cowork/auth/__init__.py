"""认证模块 (v0.4.0 Stage 2) — JWT 校验 + 静态 token fallback。

- JWTVerifier (jwt.py): PyJWT 校验 HS256/RS256, 从 claim 提取 tenant_id/user_id。
- verify_static_token (fallback.py): 本地/桌面保留的静态 token fallback, 生产强制 JWT。
- IdentityClient (identity.py, issue #88): fusion-identity 集成 — 统一 JWT 签发 + 租户注册中心。

不泄 token 原文到日志 (仅记校验失败原因 + token 前 8 char 指纹)。
"""

from fusion_cowork.auth.fallback import require_jwt, verify_any_token, verify_static_token
from fusion_cowork.auth.identity import (
    IdentityClient,
    IdentityVerifyResult,
    get_identity_client,
    is_identity_enabled,
    make_verify_jwt_callback,
    reset_identity_client,
)
from fusion_cowork.auth.jwt import JWTVerifier, get_default_verifier

__all__ = [
    "JWTVerifier",
    "get_default_verifier",
    "verify_any_token",
    "verify_static_token",
    "require_jwt",
    "IdentityClient",
    "IdentityVerifyResult",
    "is_identity_enabled",
    "get_identity_client",
    "make_verify_jwt_callback",
    "reset_identity_client",
]
