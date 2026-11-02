from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

DEFAULT_REDIRECT_URI = "http://127.0.0.1:11461/api/capabilities/oauth/callback"
STATE_TTL = 600
_TOKEN_KEYS = ("access_token", "refresh_token", "token_type", "expires_in", "scope")


class OAuthError(Exception):
    pass


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class OAuthFlow:
    def __init__(self, *, transport: Any = None, timeout: float = 10.0, redirect_uri: str = ""):
        self._transport = transport
        self._timeout = timeout
        self._redirect_uri = redirect_uri or DEFAULT_REDIRECT_URI
        self._pending: Dict[str, Dict[str, Any]] = {}

    def _prune(self) -> None:
        now = time.time()
        stale = [s for s, p in self._pending.items() if now - p["created_at"] > STATE_TTL]
        for state in stale:
            self._pending.pop(state, None)
        if stale:
            logger.info(f"OAuth 状态清理: {len(stale)} 条过期")

    def start(self, connector_id: str, oauth_cfg: Dict[str, Any], *, workspace_id: str) -> Dict[str, Any]:
        client_id_env = str(oauth_cfg.get("client_id_env") or "")
        client_id = os.environ.get(client_id_env, "").strip() if client_id_env else ""
        if not client_id:
            raise OAuthError(f"oauth_not_configured: {client_id_env or 'client_id_env'} 未设置")
        authorize_url = str(oauth_cfg.get("authorize_url") or "")
        if not authorize_url:
            raise OAuthError("oauth_not_configured: authorize_url 缺失")
        self._prune()
        state = secrets.token_urlsafe(24)
        scopes = [str(s) for s in oauth_cfg.get("scopes") or []]
        params: Dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": str(oauth_cfg.get("redirect_uri") or self._redirect_uri),
            "state": state,
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        verifier = ""
        if oauth_cfg.get("pkce", True):
            verifier = secrets.token_urlsafe(48)
            params["code_challenge"] = _pkce_challenge(verifier)
            params["code_challenge_method"] = "S256"
        self._pending[state] = {
            "connector_id": connector_id,
            "workspace_id": workspace_id,
            "created_at": time.time(),
            "verifier": verifier,
            "scopes": scopes,
            "redirect_uri": params["redirect_uri"],
        }
        separator = "&" if "?" in authorize_url else "?"
        logger.info(f"OAuth 授权开始: connector={connector_id} ws={workspace_id} scopes={scopes}")
        return {
            "authorize_url": f"{authorize_url}{separator}{urlencode(params)}",
            "state": state,
            "expires_in": STATE_TTL,
        }

    def pop_state(self, state: str) -> Optional[Dict[str, Any]]:
        self._prune()
        pending = self._pending.pop(str(state or ""), None)
        if pending is None:
            logger.warning("OAuth 回调 state 未知或已过期")
        return pending

    async def exchange(self, pending: Dict[str, Any], code: str, oauth_cfg: Dict[str, Any]) -> Dict[str, Any]:
        import httpx

        token_url = str(oauth_cfg.get("token_url") or "")
        if not token_url:
            raise OAuthError("oauth_not_configured: token_url 缺失")
        client_id_env = str(oauth_cfg.get("client_id_env") or "")
        client_id = os.environ.get(client_id_env, "").strip()
        secret_env = str(oauth_cfg.get("client_secret_env") or "")
        client_secret = os.environ.get(secret_env, "").strip() if secret_env else ""
        data: Dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": str(pending.get("redirect_uri") or self._redirect_uri),
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        if pending.get("verifier"):
            data["code_verifier"] = pending["verifier"]
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            resp = await client.post(token_url, data=data, headers={"Accept": "application/json"})
            if resp.status_code >= 400:
                logger.warning(f"OAuth token 交换失败: {token_url} status={resp.status_code}")
                raise OAuthError(f"token_exchange_failed: {resp.status_code}")
            payload = resp.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise OAuthError("token_exchange_failed: access_token 缺失")
        tokens = {k: payload[k] for k in _TOKEN_KEYS if k in payload}
        logger.info(f"OAuth token 已获取: connector={pending.get('connector_id')} keys={sorted(tokens)} (值不落日志)")
        return tokens
