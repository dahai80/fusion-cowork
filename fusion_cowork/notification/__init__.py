"""移动推送通知包 — P2-5。

跨平台移动推送 (Bark / ntfy), 未配置时降级到本地 macOS 通知。
"""

from .push import (
    PushConfig,
    PushResult,
    push,
    resolve_config,
)

__all__ = [
    "PushConfig",
    "PushResult",
    "push",
    "resolve_config",
]
