# 111: process-wide LifecycleBus used by the workflow event emitter and the
# space chat LLM funnel. In-memory by default (bounded ring per session); set
# FUSION_COWORK_LIFECYCLE_DIR to persist events as JSONL for replay.

from __future__ import annotations

import logging
import os

from .funnels import KERNEL_SOURCE, LifecycleBus

logger = logging.getLogger(__name__)

_bus: LifecycleBus | None = None


def get_lifecycle_bus() -> LifecycleBus:
    global _bus
    if _bus is None:
        jsonl_dir = os.environ.get("FUSION_COWORK_LIFECYCLE_DIR") or None
        _bus = LifecycleBus(jsonl_dir=jsonl_dir)
        logger.info(f"LifecycleBus initialized kernel={KERNEL_SOURCE} jsonl_dir={jsonl_dir or '-'}")
    return _bus


def reset_lifecycle_bus() -> None:
    global _bus
    _bus = None
    logger.info("LifecycleBus reset")


__all__ = ["get_lifecycle_bus", "reset_lifecycle_bus"]
