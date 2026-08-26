from .audit import AuditLog
from .circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState

# encryption 依赖 cryptography (cloud extra); 模块内惰性 import, 这里 re-export 函数名安全
from .encryption import (
    decrypt_at_rest,
    derive_key,
    encrypt_at_rest,
    get_encryption_key,
    is_encrypted,
)
from .quotas import (
    QuotaEnforcer,
    QuotaExceededError,
    TenantQuotas,
    get_default_quota_enforcer,
)
from .rate_limit import (
    FastAPIRateLimitMiddleware,
    RateLimiter,
    TokenBucket,
    get_default_rate_limiter,
)
from .scoped_folder import (
    ScopedFolderManager,
    get_scoped_folder_manager,
    reset_scoped_folder_manager,
)

__all__ = [
    "ScopedFolderManager",
    "get_scoped_folder_manager",
    "reset_scoped_folder_manager",
    "TokenBucket",
    "RateLimiter",
    "FastAPIRateLimitMiddleware",
    "get_default_rate_limiter",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "AuditLog",
    "encrypt_at_rest",
    "decrypt_at_rest",
    "derive_key",
    "get_encryption_key",
    "is_encrypted",
    "TenantQuotas",
    "QuotaEnforcer",
    "QuotaExceededError",
    "get_default_quota_enforcer",
]
