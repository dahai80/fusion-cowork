# 111: fusion-harness kernel shim for context compaction.
# #190/#2: fusion-harness is a hard dependency — no in-tree fallback.

from __future__ import annotations

from fusion_harness.compactor import (
    CompactionConfig,
    Compactor,
    RetentionList,
)

__all__ = ["CompactionConfig", "Compactor", "RetentionList"]
