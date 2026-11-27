"""Mobile push notification package — P2-5.

Cross-platform mobile push (Bark / ntfy), degrades to local macOS notification when unconfigured.
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
