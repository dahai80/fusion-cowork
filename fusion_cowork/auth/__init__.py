"""Auth module (v0.4.0 Stage 2) — JWT verification + static token fallback.

- JWTVerifier (jwt.py): PyJWT verification HS256/RS256, extracts tenant_id/user_id from claims.
- verify_static_token (fallback.py): static token fallback kept for local/desktop, JWT enforced in production.
- IdentityClient (identity.py, issue #88): fusion-identity integration — unified JWT issuance + tenant registry.

Never leaks the raw token to logs (only logs the verification failure reason + the token's first 8 chars as a fingerprint).
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
from fusion_cowork.auth.identity_propagation import (
    extract_identity_token,
    friendly_quota_error,
    get_current_identity_token,
    identity_auth_headers,
    identity_token_context,
    set_current_identity_token,
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
    "extract_identity_token",
    "friendly_quota_error",
    "get_current_identity_token",
    "identity_auth_headers",
    "identity_token_context",
    "set_current_identity_token",
]
