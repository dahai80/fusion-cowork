from __future__ import annotations

import contextvars
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "default"
LOCAL_USER = "local_user"

_tenant_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("fusion_tenant_id", default=DEFAULT_TENANT)
_user_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("fusion_user_id", default=LOCAL_USER)


@dataclass(frozen=True)
class TenantPrincipal:
    tenant_id: str = DEFAULT_TENANT
    user_id: str = LOCAL_USER

    @classmethod
    def from_context(cls) -> TenantPrincipal:
        return cls(tenant_id=_tenant_ctx.get(), user_id=_user_ctx.get())

    @property
    def is_local(self) -> bool:
        return self.tenant_id == DEFAULT_TENANT and self.user_id == LOCAL_USER


def get_current_tenant() -> str:
    return _tenant_ctx.get()


def set_current_tenant(tenant_id: str) -> contextvars.Token[str]:
    if not tenant_id:
        tenant_id = DEFAULT_TENANT
    logger.debug("set tenant_id=%s", tenant_id)
    return _tenant_ctx.set(tenant_id)


def reset_current_tenant(token: contextvars.Token[str]) -> None:
    _tenant_ctx.reset(token)


def get_current_user() -> str:
    return _user_ctx.get()


def set_current_user(user_id: str) -> contextvars.Token[str]:
    if not user_id:
        user_id = LOCAL_USER
    return _user_ctx.set(user_id)


def reset_current_user(token: contextvars.Token[str]) -> None:
    _user_ctx.reset(token)


@asynccontextmanager
async def tenant_context(tenant_id: str, user_id: Optional[str] = None) -> AsyncIterator[TenantPrincipal]:
    t_tok = set_current_tenant(tenant_id)
    u_tok = set_current_user(user_id or LOCAL_USER)
    principal = TenantPrincipal(tenant_id=get_current_tenant(), user_id=get_current_user())
    logger.debug("enter tenant_context tenant=%s user=%s", principal.tenant_id, principal.user_id)
    try:
        yield principal
    finally:
        reset_current_tenant(t_tok)
        reset_current_user(u_tok)
        logger.debug("exit tenant_context")


def resolve_tenant_id(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    return get_current_tenant()
