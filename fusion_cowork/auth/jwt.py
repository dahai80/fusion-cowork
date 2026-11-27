"""JWT verification (v0.4.0 Stage 2) — PyJWT HS256/RS256, extracts tenant_id/user_id from claims.

Verifies externally issued tokens:
- HS256: env FUSION_JWT_SECRET (shared secret)
- RS256: env FUSION_JWKS_URL (remotely fetched public keys, optional cache) or FUSION_JWT_PUBLIC_KEY (PEM file/content)
- claim mapping: tenant_id (tenant_id/tid/tenant), user_id (sub/user_id/uid), scopes (scope/scopes, space-separated or list)

Failure returns None + log (never leaks the raw token; only logs an 8-char fingerprint + reason).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

from fusion_cowork.tenant import DEFAULT_TENANT, LOCAL_USER, TenantPrincipal

logger = logging.getLogger(__name__)

_DEFAULT_VERIFIER: Optional[JWTVerifier] = None
_JWKS_CACHE_TTL = 600  # 10 min


def get_default_verifier() -> JWTVerifier:
    """Singleton JWTVerifier (constructed from env config; no secret/jwks configured → empty verifier, verify_token always returns None).

    issue #88: FUSION_IDENTITY_ENABLED=1 → returns _IdentityJWTAdapter (delegating to fusion-identity /verify),
    transparently serving all get_default_verifier consumers (mcp_http/space api/rate_limit).
    """
    global _DEFAULT_VERIFIER
    if _DEFAULT_VERIFIER is None:
        try:
            from fusion_cowork.auth.identity import get_identity_client

            client = get_identity_client()
            if client is not None:
                _DEFAULT_VERIFIER = _IdentityJWTAdapter(client)
                logger.info("JWTVerifier singleton → fusion-identity adapter (FUSION_IDENTITY_ENABLED=1)")
                return _DEFAULT_VERIFIER
        except Exception as e:
            logger.warning(f"Identity adapter construction failed, falling back to local JWT: {e}")
        _DEFAULT_VERIFIER = JWTVerifier.from_env()
    return _DEFAULT_VERIFIER


class JWTVerifier:
    """JWT verifier — HS256/RS256 dual mode.

    No secret/public_key/jwks_url configured → inactive, verify_token always returns None (static fallback applies).
    """

    def __init__(
        self,
        secret: Optional[str] = None,
        public_key: Optional[str] = None,
        jwks_url: Optional[str] = None,
        algorithms: Optional[list] = None,
        issuer: Optional[str] = None,
        audience: Optional[str] = None,
        leeway: int = 0,
    ):
        self._secret = secret
        self._public_key = public_key
        self._jwks_url = jwks_url
        self._algorithms = algorithms or (["RS256"] if (public_key or jwks_url) and not secret else ["HS256"])
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway
        self._jwks_cache: Optional[Dict[str, Any]] = None
        self._jwks_fetched_at: float = 0.0
        self._active = bool(secret or public_key or jwks_url)
        if self._active:
            mode = "HS256" if secret else "RS256"
            logger.info(f"JWTVerifier enabled (alg={mode}, issuer={issuer or '-'}, aud={audience or '-'})")

    @classmethod
    def from_env(cls) -> JWTVerifier:
        """Construct from env (unconfigured → inactive)."""
        return cls(
            secret=os.environ.get("FUSION_JWT_SECRET"),
            public_key=os.environ.get("FUSION_JWT_PUBLIC_KEY"),
            jwks_url=os.environ.get("FUSION_JWKS_URL"),
            issuer=os.environ.get("FUSION_JWT_ISSUER"),
            audience=os.environ.get("FUSION_JWT_AUDIENCE"),
            leeway=int(os.environ.get("FUSION_JWT_LEEWAY", "0") or "0"),
        )

    @property
    def active(self) -> bool:
        return self._active

    async def verify_token_async(self, token: str) -> Optional[TenantPrincipal]:
        """Async verification (remote JWKS fetch may block → run in a thread pool). Returns TenantPrincipal or None."""
        import asyncio

        return await asyncio.to_thread(self.verify_token, token)

    def verify_token(self, token: str) -> Optional[TenantPrincipal]:
        """Synchronously verify token → TenantPrincipal | None. Failure logs + returns None, never raises."""
        if not self._active:
            return None
        if not token or not isinstance(token, str):
            logger.warning("JWT verification failed: token missing")
            return None
        fingerprint = token[:8] if len(token) >= 8 else "?"
        try:
            import jwt as pyjwt
        except ImportError:
            logger.error("JWT verification failed: pyjwt not installed (pip install 'fusion-cowork[cloud]')")
            return None
        decode_kwargs: Dict[str, Any] = {"algorithms": self._algorithms, "leeway": self._leeway}
        if self._issuer:
            decode_kwargs["issuer"] = self._issuer
        if self._audience:
            decode_kwargs["audience"] = self._audience
        try:
            if self._secret:
                payload = pyjwt.decode(token, self._secret, **decode_kwargs)
            elif self._public_key:
                payload = pyjwt.decode(token, self._public_key, **decode_kwargs)
            elif self._jwks_url:
                unverified = pyjwt.decode(token, options={"verify_signature": False})
                kid = unverified.get("kid", "")
                pub = self._get_jwks_key(kid)
                if not pub:
                    logger.warning(f"JWT verification failed: JWKS has no kid={kid or '-'} (fp={fingerprint})")
                    return None
                payload = pyjwt.decode(token, pub, **decode_kwargs)
            else:
                logger.warning("JWT verification failed: verifier missing secret/public_key/jwks")
                return None
        except pyjwt.ExpiredSignatureError:
            logger.warning(f"JWT verification failed: token expired (fp={fingerprint})")
            return None
        except pyjwt.InvalidTokenError as e:
            logger.warning(f"JWT verification failed: {type(e).__name__} (fp={fingerprint})")
            return None
        principal = self._extract_claims(payload)
        logger.info(
            f"JWT verification passed: tenant={principal.tenant_id} user={principal.user_id} (fp={fingerprint})"
        )
        return principal

    # ── claim extraction ──

    @staticmethod
    def _extract_claims(payload: Dict[str, Any]) -> TenantPrincipal:
        """Extract tenant_id + user_id from payload → TenantPrincipal."""
        tid = payload.get("tenant_id") or payload.get("tid") or payload.get("tenant") or DEFAULT_TENANT
        uid = payload.get("user_id") or payload.get("uid") or payload.get("sub") or LOCAL_USER
        return TenantPrincipal(tenant_id=str(tid), user_id=str(uid))

    # ── JWKS ──

    def _get_jwks_key(self, kid: str) -> Optional[str]:
        """Get the public key PEM from JWKS (cached). Missing → None."""
        if not self._jwks_url:
            return None
        now = time.time()
        if self._jwks_cache is None or (now - self._jwks_fetched_at) > _JWKS_CACHE_TTL:
            try:
                import urllib.request

                with urllib.request.urlopen(self._jwks_url, timeout=5) as resp:
                    self._jwks_cache = json.loads(resp.read().decode("utf-8"))
                self._jwks_fetched_at = now
                logger.info(f"JWKS fetch succeeded: {self._jwks_url} keys={len(self._jwks_cache.get('keys', []))}")
            except Exception as e:
                logger.warning(f"JWKS fetch failed: {e}")
                return None
        for key in self._jwks_cache.get("keys", []):
            if key.get("kid") == kid:
                try:
                    from jwt.algorithms import RSAAlgorithm

                    return RSAAlgorithm.from_jwk(json.dumps(key))
                except Exception as e:
                    logger.warning(f"JWKS key parse failed kid={kid}: {e}")
                    return None
        return None


class _IdentityJWTAdapter(JWTVerifier):
    """issue #88: fusion-identity delegation adapter — verify_token routes to IdentityClient.verify.

    Inherits JWTVerifier to keep the active/verify_token_async interface compatible;
    verify_token never decodes the JWT locally, fully delegating verification to the
    fusion-identity server (the sole issuer).
    """

    def __init__(self, identity_client):
        super().__init__()
        self._identity_client = identity_client
        self._active = True

    def verify_token(self, token: str) -> Optional[TenantPrincipal]:
        if not token or not isinstance(token, str):
            logger.warning("identity JWT verification failed: token missing")
            return None
        result = self._identity_client.verify(token)
        if result is None:
            return None
        return TenantPrincipal(tenant_id=result.tid, user_id=LOCAL_USER)
