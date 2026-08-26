"""Stage 6 — 令牌桶限流 (per-tenant 隔离)。

TokenBucket: 经典令牌桶, rate (令牌/秒) + burst (桶容量), 后台无 — 惰性按时间差补充。
RateLimiter: Dict[key, TokenBucket], allow(key) -> bool, per-key 隔离 (A 耗尽不影响 B)。
FastAPIRateLimitMiddleware: 读 request.state.tenant_id (JWT 中间件设), 超限 429。

opt-in: 无显式配置 (RateLimiter 实例) → 不限流, 现有行为不变。
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TokenBucket:
    """令牌桶 — rate 令牌/秒, burst 桶容量上限。惰性补充 (allow 时按经过时间补)。"""

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
    """per-key 限流器 — 默认无限 (unlimited=False 时按 key 各建独立桶)。"""

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
            logger.debug(f"RateLimiter 新桶 key={key} rate={self.rate} burst={self.burst}")
        ok = bucket.allow()
        if not ok:
            logger.warning(f"RateLimiter 限流命中 key={key} (rate={self.rate}/s burst={self.burst})")
        return ok

    def reset(self, key: str = "") -> None:
        if key:
            self._buckets.pop(key, None)
        else:
            self._buckets.clear()


class FastAPIRateLimitMiddleware:
    """ASGI 中间件 — 读 request.state.tenant_id, 超限 429。

    用法: app = FastAPIRateLimitMiddleware(app, limiter)
    缺 limiter 或 limiter.unlimited → 透传不限。
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
        # 优先读 scope["state"].tenant_id (route handler 注入); 缺则从 Authorization JWT 提。
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
    """从 Authorization: Bearer <jwt> 提 tenant_id (JWT verifier active 时); 否则空。"""
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
    """从 env 构造默认限流器: FUSION_RATE_LIMIT (rate/burst 如 10/20) 设了才启用。"""
    import os

    cfg = os.environ.get("FUSION_RATE_LIMIT", "").strip()
    if not cfg:
        return RateLimiter(unlimited=True)
    try:
        parts = cfg.split("/")
        rate = float(parts[0])
        burst = int(parts[1]) if len(parts) > 1 else int(rate * 2)
        logger.info(f"默认限流器启用 rate={rate}/s burst={burst}")
        return RateLimiter(rate=rate, burst=burst)
    except (ValueError, IndexError):
        logger.warning(f"FUSION_RATE_LIMIT 格式错 '{cfg}' (期望 rate/burst 如 10/20), 限流未启")
        return RateLimiter(unlimited=True)
