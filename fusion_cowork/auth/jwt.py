"""JWT 校验 (v0.4.0 Stage 2) — PyJWT HS256/RS256, 从 claim 提取 tenant_id/user_id。

校验外部签发 token:
- HS256: env FUSION_JWT_SECRET (shared secret)
- RS256: env FUSION_JWKS_URL (远程拉取公钥, 可选 cache) 或 FUSION_JWT_PUBLIC_KEY (PEM 文件/内容)
- claim 映射: tenant_id (tenant_id/tid/tenant)、user_id (sub/user_id/uid)、scopes (scope/scopes, 空格或列表)

失败返 None + 日志 (不泄 token 原文, 仅记前 8 char 指纹 + 原因)。
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
    """单例 JWTVerifier (按 env 配置构造, 未配 secret/jwks → 空 verifier, verify_token 恒返 None)。"""
    global _DEFAULT_VERIFIER
    if _DEFAULT_VERIFIER is None:
        _DEFAULT_VERIFIER = JWTVerifier.from_env()
    return _DEFAULT_VERIFIER


class JWTVerifier:
    """JWT 校验器 — HS256/RS256 双模。

    未配 secret/public_key/jwks_url → inactive, verify_token 恒返 None (走静态 fallback)。
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
            logger.info(f"JWTVerifier 已启用 (alg={mode}, issuer={issuer or '-'}, aud={audience or '-'})")

    @classmethod
    def from_env(cls) -> JWTVerifier:
        """从 env 构造 (未配 → inactive)。"""
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
        """异步校验 (JWKS 远程拉取可能阻塞 → 线程池跑)。返 TenantPrincipal 或 None。"""
        import asyncio

        return await asyncio.to_thread(self.verify_token, token)

    def verify_token(self, token: str) -> Optional[TenantPrincipal]:
        """同步校验 token → TenantPrincipal | None。失败日志 + None, 不抛。"""
        if not self._active:
            return None
        if not token or not isinstance(token, str):
            logger.warning("JWT 校验失败: token 缺失")
            return None
        fingerprint = token[:8] if len(token) >= 8 else "?"
        try:
            import jwt as pyjwt
        except ImportError:
            logger.error("JWT 校验失败: pyjwt 未安装 (pip install 'fusion-cowork[cloud]')")
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
                    logger.warning(f"JWT 校验失败: JWKS 无 kid={kid or '-'} (fp={fingerprint})")
                    return None
                payload = pyjwt.decode(token, pub, **decode_kwargs)
            else:
                logger.warning("JWT 校验失败: verifier 未配 secret/public_key/jwks")
                return None
        except pyjwt.ExpiredSignatureError:
            logger.warning(f"JWT 校验失败: token 过期 (fp={fingerprint})")
            return None
        except pyjwt.InvalidTokenError as e:
            logger.warning(f"JWT 校验失败: {type(e).__name__} (fp={fingerprint})")
            return None
        principal = self._extract_claims(payload)
        logger.info(f"JWT 校验通过: tenant={principal.tenant_id} user={principal.user_id} (fp={fingerprint})")
        return principal

    # ── claim 提取 ──

    @staticmethod
    def _extract_claims(payload: Dict[str, Any]) -> TenantPrincipal:
        """从 payload 提取 tenant_id + user_id → TenantPrincipal。"""
        tid = payload.get("tenant_id") or payload.get("tid") or payload.get("tenant") or DEFAULT_TENANT
        uid = payload.get("user_id") or payload.get("uid") or payload.get("sub") or LOCAL_USER
        return TenantPrincipal(tenant_id=str(tid), user_id=str(uid))

    # ── JWKS ──

    def _get_jwks_key(self, kid: str) -> Optional[str]:
        """从 JWKS (cache) 取公钥 PEM。无 → None。"""
        if not self._jwks_url:
            return None
        now = time.time()
        if self._jwks_cache is None or (now - self._jwks_fetched_at) > _JWKS_CACHE_TTL:
            try:
                import urllib.request

                with urllib.request.urlopen(self._jwks_url, timeout=5) as resp:
                    self._jwks_cache = json.loads(resp.read().decode("utf-8"))
                self._jwks_fetched_at = now
                logger.info(f"JWKS 拉取成功: {self._jwks_url} keys={len(self._jwks_cache.get('keys', []))}")
            except Exception as e:
                logger.warning(f"JWKS 拉取失败: {e}")
                return None
        for key in self._jwks_cache.get("keys", []):
            if key.get("kid") == kid:
                try:
                    from jwt.algorithms import RSAAlgorithm

                    return RSAAlgorithm.from_jwk(json.dumps(key))
                except Exception as e:
                    logger.warning(f"JWKS key 解析失败 kid={kid}: {e}")
                    return None
        return None
