"""Mobile push notification package - P2-5.

Cross-platform mobile push (Bark / ntfy); falls back to local macOS
notifications when unconfigured.
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
