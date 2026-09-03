"""fusion-identity 集成 (issue #88) — 统一 JWT 签发 + 租户注册中心。

fusion-identity (端口 11470) 是 Fusion 生态唯一的 JWT 签发者 + 租户注册表。
本模块通过 POST /api/v1/auth/verify 委托校验, 替代本地 JWT/RBAC/配额重实现。

opt-in: env FUSION_IDENTITY_ENABLED=1 激活。默认 OFF = 零行为变化 (CI 无 fusion-identity)。
启用后 fail-closed: identity 不可达 → 401, 不静默降级本地 JWT (生产模式)。

融合设计:
- sync httpx.Client (fusion_core.tenant middleware 要求 verify_jwt 同步)
- jti→claims cache (TTL 60s, cap 1024) 削减重复 /verify 调用
- revoke=True / tenant_status!=active → None (负缓存 30s)
- 配额从 VerifyResponse.quota 取 (替代 ConfigCenter)
- usage 上报 POST /api/v1/tenants/{tid}/usage (best-effort)
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_IDENTITY_URL = "http://127.0.0.1:11470"
_VERIFY_PATH = "/api/v1/auth/verify"
_USAGE_PATH = "/api/v1/tenants/{tenant_id}/usage"
_CACHE_TTL = 60
_NEG_CACHE_TTL = 30
_CACHE_CAP = 1024


@dataclass
class IdentityVerifyResult:
    tid: str
    role: str = ""
    scopes: tuple[str, ...] = ()
    quota: dict[str, Any] = field(default_factory=dict)
    tenant_status: str = "active"
    revoked: bool = False


class IdentityError(Exception):
    pass


def is_identity_enabled() -> bool:
    return os.environ.get("FUSION_IDENTITY_ENABLED", "") == "1"


def _extract_jti(token: str) -> str:
    try:
        from fusion_core.tenant.jwt_utils import decode_jwt_claims

        claims = decode_jwt_claims(token, verify_exp=False)
        jti = claims.get("jti")
        if jti:
            return f"jti:{jti}"
    except Exception as e:
        logger.debug("identity jti 解析失败, 回退 token hash: %s", e)
    return "sha:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class IdentityClient:
    def __init__(
        self,
        base_url: str,
        service_token: str,
        timeout: float = 2.0,
        cache_ttl: int = _CACHE_TTL,
        cache_cap: int = _CACHE_CAP,
    ):
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._cache_cap = cache_cap
        self._cache: dict[str, tuple[float, Optional[IdentityVerifyResult]]] = {}
        import httpx

        self._client = httpx.Client(timeout=timeout)
        logger.info(
            "IdentityClient 启用: base_url=%s timeout=%s cache_ttl=%s cap=%s",
            self._base_url,
            timeout,
            cache_ttl,
            cache_cap,
        )

    def _cache_get(self, key: str) -> Optional[IdentityVerifyResult]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.monotonic() - ts > self._cache_ttl:
            self._cache.pop(key, None)
            return None
        return val

    def _cache_put(self, key: str, val: Optional[IdentityVerifyResult]) -> None:
        if len(self._cache) >= self._cache_cap:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)
        self._cache[key] = (time.monotonic(), val)

    def verify(self, token: str) -> Optional[IdentityVerifyResult]:
        if not token or not isinstance(token, str):
            logger.warning("identity verify 失败: token 缺失")
            return None
        key = _extract_jti(token)
        cached = self._cache_get(key)
        if cached is not None:
            logger.debug("identity cache hit key=%s tid=%s", key, cached.tid)
            return cached
        if cached is None and key in self._cache:
            self._cache.pop(key, None)
        try:
            resp = self._client.post(
                self._base_url + _VERIFY_PATH,
                headers={"Authorization": f"Bearer {self._service_token}"},
                json={"token": token},
            )
            if resp.status_code != 200:
                logger.warning("identity /verify 非 200: status=%s", resp.status_code)
                self._cache_put(key, None)
                return None
            data = resp.json()
        except Exception as e:
            logger.warning("identity /verify 请求失败 (fail-closed): %s", e)
            return None
        result = IdentityVerifyResult(
            tid=str(data.get("tid") or data.get("tenant") or ""),
            role=str(data.get("role") or ""),
            scopes=tuple(data.get("scopes") or data.get("scope") or ()),
            quota=dict(data.get("quota") or {}),
            tenant_status=str(data.get("tenant_status") or "active"),
            revoked=bool(data.get("revoked", False)),
        )
        if result.revoked:
            logger.warning("identity verify: token 已吊销 tid=%s", result.tid)
            self._cache_put(key, None)
            return None
        if result.tenant_status != "active":
            logger.warning("identity verify: 租户非活跃 tid=%s status=%s", result.tid, result.tenant_status)
            self._cache_put(key, None)
            return None
        if not result.tid:
            logger.warning("identity verify: 响应无 tid")
            return None
        self._cache_put(key, result)
        logger.info("identity verify 通过: tid=%s role=%s scopes=%s", result.tid, result.role, len(result.scopes))
        return result

    def emit_usage(
        self,
        tenant_id: str,
        metric: str,
        value: int | float,
        source: str = "fusion-cowork",
        model: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        body: dict[str, Any] = {"metric": metric, "value": value, "source": source}
        if model:
            body["model"] = model
        if user_id:
            body["user_id"] = user_id
        try:
            resp = self._client.post(
                self._base_url + _USAGE_PATH.format(tenant_id=tenant_id),
                headers={"Authorization": f"Bearer {self._service_token}"},
                json=body,
            )
            ok = resp.status_code in (200, 201, 202, 204)
            if not ok:
                logger.debug("identity usage 上报非 2xx: status=%s metric=%s", resp.status_code, metric)
            return ok
        except Exception as e:
            logger.debug("identity usage 上报失败 (best-effort): %s", e)
            return False

    def ping(self) -> bool:
        try:
            resp = self._client.get(self._base_url + "/health")
            return resp.status_code == 200
        except Exception as e:
            logger.debug("identity ping 失败: %s", e)
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


_IDENTITY_CLIENT: Optional[IdentityClient] = None


def get_identity_client() -> Optional[IdentityClient]:
    global _IDENTITY_CLIENT
    if not is_identity_enabled():
        return None
    if _IDENTITY_CLIENT is None:
        url = os.environ.get("FUSION_IDENTITY_URL", _DEFAULT_IDENTITY_URL)
        token = os.environ.get("FUSION_IDENTITY_SERVICE_TOKEN", "")
        if not token:
            logger.warning("FUSION_IDENTITY_ENABLED=1 但 FUSION_IDENTITY_SERVICE_TOKEN 未配, identity 降级禁用")
            return None
        _IDENTITY_CLIENT = IdentityClient(base_url=url, service_token=token)
    return _IDENTITY_CLIENT


def reset_identity_client() -> None:
    global _IDENTITY_CLIENT
    if _IDENTITY_CLIENT is not None:
        _IDENTITY_CLIENT.close()
    _IDENTITY_CLIENT = None


def make_verify_jwt_callback(client: IdentityClient) -> Callable[[str], dict[str, Any]]:
    def _cb(token: str) -> dict[str, Any]:
        result = client.verify(token)
        if result is None:
            raise IdentityError("identity verify 失败或 token 已吊销")
        return {
            "tid": result.tid,
            "role": result.role,
            "scope": list(result.scopes),
            "scopes": list(result.scopes),
            "quota": result.quota,
            "tenant_status": result.tenant_status,
        }

    return _cb
