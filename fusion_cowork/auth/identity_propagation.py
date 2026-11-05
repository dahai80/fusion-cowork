"""issue #288: identity JWT propagation for gateway quota metering (cloud-try Phase 2).

The fusion-gateway meter middleware keys per-user daily token quota by the
identity JWT `sub` on each LLM call. Cowork web backend captures the caller's
identity JWT (forwarded from the shell session via the SDK bridge) in a
contextvar and FusionMLXClient attaches it as `Authorization: Bearer <jwt>`
on outbound LLM calls. Also owns the friendly quota-exceeded error mapping
for the gateway `429 {"detail": "daily token quota exceeded", "quota": {...}}` shape.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_IDENTITY_TOKEN: ContextVar[str] = ContextVar("fusion_identity_token", default="")

_IDENTITY_HEADER = "x-fusion-identity-token"


@contextmanager
def identity_token_context(token: str):
    # Set the caller identity token for the duration of the block. Used by the
    # web API middleware (in-request LLM calls) and RunRegistry._execute
    # (background workflow runs — the coroutine inherits the context).
    tok = _IDENTITY_TOKEN.set(str(token or ""))
    try:
        yield
    finally:
        _IDENTITY_TOKEN.reset(tok)


def get_current_identity_token() -> str:
    return _IDENTITY_TOKEN.get()


def set_current_identity_token(token: str) -> object:
    # Fire-and-forget setter for dedicated task contexts (RunRegistry._execute
    # background coroutine) — the task's context dies with the task, no reset.
    return _IDENTITY_TOKEN.set(str(token or ""))


def looks_like_jwt(token: str) -> bool:
    # A fusion-identity JWT has the classic three dot-separated segments.
    # Module API keys (fm-...) never do, so this separates the two bearer kinds.
    parts = str(token or "").split(".")
    return len(parts) == 3 and all(p.strip() for p in parts)


def extract_identity_token(headers: Dict[str, str]) -> str:
    # Priority: explicit X-Fusion-Identity-Token header, then an Authorization
    # bearer that looks like a JWT (inbound module auth uses the fm- key on the
    # same header, so shape — not presence — decides).
    token = str(headers.get(_IDENTITY_HEADER) or headers.get(_IDENTITY_HEADER.title()) or "").strip()
    if token:
        return token
    auth = str(headers.get("authorization") or headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        candidate = auth[7:].strip()
        if looks_like_jwt(candidate):
            return candidate
    return ""


def identity_auth_headers() -> Dict[str, str]:
    # Per-request headers for the LLM call — overrides the client default
    # Authorization only when the caller's identity token is present.
    token = get_current_identity_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def friendly_quota_error(status_code: int, body: Any) -> Optional[str]:
    # Map the gateway meter 429 quota shape to a bilingual operator-friendly
    # message; anything else returns None (caller keeps the raw error path).
    if status_code != 429:
        return None
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("detail") or "")
    elif isinstance(body, str):
        detail = body
    if "quota" not in detail.lower():
        return None
    logger.warning(f"LLM 调用被网关配额拒绝: {detail}")
    return (
        "每日 token 配额已用尽 (daily token quota exceeded) — 请明日再试或联系管理员提升预算; "
        f"quota={body.get('quota') if isinstance(body, dict) else ''}"
    )
