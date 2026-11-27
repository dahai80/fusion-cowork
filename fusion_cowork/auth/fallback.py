"""Static token fallback (v0.4.0 Stage 2) — kept for local/desktop, JWT enforced in production.

- verify_static_token(token, expected): equality comparison (constant time), returns TenantPrincipal (default tenant/local_user)
- require_jwt(): env FUSION_REQUIRE_JWT=1 → production mode, static fallback disabled
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fusion_cowork.tenant import DEFAULT_TENANT, LOCAL_USER, TenantPrincipal

logger = logging.getLogger(__name__)


def require_jwt() -> bool:
    """Production mode switch: env FUSION_REQUIRE_JWT=1 → JWT enforced, static fallback disabled."""
    return os.environ.get("FUSION_REQUIRE_JWT", "") == "1"


def verify_static_token(token: Optional[str], expected: Optional[str]) -> Optional[TenantPrincipal]:
    """Static token verification (local/desktop).

    expected configured → token must be constant-time equal, on pass returns the default
    principal (local_user/default tenant);
    expected not configured (local, no auth) → unconditionally returns the default principal
    (single-user back-compat);
    require_jwt()=1 and expected not configured → returns None (production enforces JWT, no static fallback).
    Failure returns None + log (never leaks the raw token).
    """
    if require_jwt() and not expected:
        logger.warning("Production mode FUSION_REQUIRE_JWT=1 with no static token, refusing degraded auth")
        return None
    if not expected:
        return TenantPrincipal(tenant_id=DEFAULT_TENANT, user_id=LOCAL_USER)
    if not token or not isinstance(token, str):
        logger.warning("Static token verification failed: token missing")
        return None
    if not hmac.compare_digest(token, expected):
        logger.warning("Static token verification failed: token mismatch")
        return None
    return TenantPrincipal(tenant_id=DEFAULT_TENANT, user_id=LOCAL_USER)


def verify_any_token(token: Optional[str], expected: Optional[str]) -> Optional[TenantPrincipal]:
    """Dual-mode verification: JWT first, static token fallback. Shared by WS/sync/remote.

    issue #88: FUSION_IDENTITY_ENABLED=1 → fusion-identity /verify first,
    revoked/fail-closed → None (production never silently falls back to a static token).
    Disabled → the current JWT-then-static path is unchanged.

    - JWT/identity verifier active + token is a valid JWT → returns the JWT claim principal
    - Otherwise static token verification (verify_static_token)
    - require_jwt()=1 + no JWT pass + no static expected → None (production enforcement)
    """
    if token and isinstance(token, str):
        try:
            from fusion_cowork.auth.jwt import get_default_verifier

            verifier = get_default_verifier()
            if verifier.active:
                jwt_principal = verifier.verify_token(token)
                if jwt_principal is not None:
                    return jwt_principal
                # issue #88: identity enabled but verification failed → fail-closed (no static token fallback)
                from fusion_cowork.auth.identity import is_identity_enabled

                if is_identity_enabled() and require_jwt():
                    logger.warning("identity verification not passed + production mode, refusing static fallback")
                    return None
        except Exception as e:
            logger.warning(f"JWT/identity verification error, falling back to static token: {e}")
    return verify_static_token(token, expected)
