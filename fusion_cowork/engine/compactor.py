# 111: fusion-harness kernel shim for context compaction.
# The kernel is a local sibling package (not on PyPI); `.[harness]` installs it.
# CI installs `.[test]` only, so the in-tree verbatim copy is the fallback and
# KERNEL_SOURCE records which path this process loaded.

from __future__ import annotations

try:
    from fusion_harness.compactor import (
        CompactionConfig,
        Compactor,
        RetentionList,
    )

    KERNEL_SOURCE = "fusion-harness"
except ImportError:
    from ._compactor_local import (
        CompactionConfig,
        Compactor,
        RetentionList,
    )

    KERNEL_SOURCE = "local"

__all__ = ["KERNEL_SOURCE", "CompactionConfig", "Compactor", "RetentionList"]
