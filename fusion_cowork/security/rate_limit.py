"""Stage 6 — token bucket rate limiting (per-tenant isolation).

TokenBucket: classic token bucket, rate (tokens/second) + burst (bucket capacity), no background
worker — lazily refilled by elapsed time.
RateLimiter: Dict[key, TokenBucket], allow(key) -> bool, per-key isolation (exhausting A does not affect B).
FastAPIRateLimitMiddleware: reads request.state.tenant_id (set by the JWT middleware), 429 on limit.

opt-in: no explicit configuration (RateLimiter instance) → no rate limiting, existing behavior unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TokenBucket:
    """Token bucket — rate tokens/second, burst bucket capacity cap. Lazily refilled (by elapsed time on allow)."""

    def __init__(self, rate: float, burst: int):
        self.rate = max(0.0, float(rate))
        self.burst = max(1, int(burst))
        self.tokens = float(self.burst)
        self._last = time.monotonic()

    def allow(self) -> bool:
        if self.rate <= 0:
            return True
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    """Per-key rate limiter — unlimited by default (when unlimited=False, each key gets its own bucket)."""

    def __init__(self, rate: float = 10.0, burst: int = 20, unlimited: bool = False):
        self.rate = rate
        self.burst = burst
        self.unlimited = unlimited
        self._buckets: Dict[str, TokenBucket] = {}

    def allow(self, key: str) -> bool:
        if self.unlimited:
            return True
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self.rate, self.burst)
            self._buckets[key] = bucket
            logger.debug(f"RateLimiter new bucket key={key} rate={self.rate} burst={self.burst}")
        ok = bucket.allow()
        if not ok:
            logger.warning(f"RateLimiter limit hit key={key} (rate={self.rate}/s burst={self.burst})")
        return ok

    def reset(self, key: str = "") -> None:
        if key:
            self._buckets.pop(key, None)
        else:
            self._buckets.clear()


class FastAPIRateLimitMiddleware:
    """ASGI middleware — reads request.state.tenant_id, 429 on limit.

    Usage: app = FastAPIRateLimitMiddleware(app, limiter)
    Missing limiter or limiter.unlimited → passthrough without limiting.
    """

    def __init__(self, app, limiter: Optional[RateLimiter] = None):
        self.app = app
        self.limiter = limiter

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.limiter is None or self.limiter.unlimited:
            await self.app(scope, receive, send)
            return
        tenant_id = "anonymous"
        # Prefer scope["state"].tenant_id (injected by route handler); fall back to extracting from the Authorization JWT.
        state = scope.get("state")
        if state is not None and hasattr(state, "tenant_id"):
            tenant_id = getattr(state, "tenant_id", "") or ""
        if not tenant_id or tenant_id == "anonymous":
            tenant_id = _extract_tenant_from_headers(scope) or "anonymous"
        if not self.limiter.allow(tenant_id):
            await send(
                {"type": "http.response.start", "status": 429, "headers": [[b"content-type", b"application/json"]]}
            )
            await send({"type": "http.response.body", "body": b'{"error":"rate limit exceeded"}'})
            return
        await self.app(scope, receive, send)


def _extract_tenant_from_headers(scope) -> str:
    """Extract tenant_id from Authorization: Bearer <jwt> (when the JWT verifier is active); otherwise empty."""
    auth = b""
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            auth = v
            break
    if not auth:
        return ""
    try:
        parts = auth.decode("latin-1").split(" ", 1)
        bearer = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
        if not bearer:
            return ""
        from fusion_cowork.auth import get_default_verifier

        verifier = get_default_verifier()
        if verifier.active:
            principal = verifier.verify_token(bearer)
            if principal is not None:
                return getattr(principal, "tenant_id", "") or ""
    except Exception:
        pass
    return ""


def get_default_rate_limiter() -> RateLimiter:
    """Build the default rate limiter from env: enabled only when FUSION_RATE_LIMIT (rate/burst e.g. 10/20) is set."""
    import os

    cfg = os.environ.get("FUSION_RATE_LIMIT", "").strip()
    if not cfg:
        return RateLimiter(unlimited=True)
    try:
        parts = cfg.split("/")
        rate = float(parts[0])
        burst = int(parts[1]) if len(parts) > 1 else int(rate * 2)
        logger.info(f"Default rate limiter enabled rate={rate}/s burst={burst}")
        return RateLimiter(rate=rate, burst=burst)
    except (ValueError, IndexError):
        logger.warning(
            f"FUSION_RATE_LIMIT malformed '{cfg}' (expected rate/burst e.g. 10/20), rate limiting not enabled"
        )
        return RateLimiter(unlimited=True)
