"""推送出口 — 任务完成 / 审批请求外发 (issue #154)。

PushEgress: webhook egress, best-effort fire-and-forget:
- 未配 FUSION_COWORK_PUSH_WEBHOOK_URL → 空实现 (零行为变化)
- 配置后: run 终态 + 审批入队事件 POST 到 webhook, 失败仅记日志 (不阻塞主流程)
- 密钥: FUSION_COWORK_PUSH_WEBHOOK_SECRET → X-Fusion-Push-Secret 头
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

_PUSH_TIMEOUT = 5.0


class PushEgress:
    def __init__(self, url: str = "", secret: str = "", transport: Any = None):
        self.url = url
        self.secret = secret
        # transport: httpx.AsyncClient 兼容注入 (测试 MockTransport), 生产 None 自建
        self._transport = transport

    @classmethod
    def from_env(cls) -> PushEgress:
        return cls(
            url=os.environ.get("FUSION_COWORK_PUSH_WEBHOOK_URL", ""),
            secret=os.environ.get("FUSION_COWORK_PUSH_WEBHOOK_SECRET", ""),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def push_event(self, kind: str, payload: Dict[str, Any]) -> bool:
        """外发一条事件。返回是否真实送达 (未启用/失败均 False, 不抛)。"""
        if not self.enabled:
            logger.debug(f"push 未启用, 丢弃事件: {kind}")
            return False
        body = {"kind": kind, "payload": payload, "ts": time.time()}
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Fusion-Push-Secret"] = self.secret
        try:
            import httpx

            async with httpx.AsyncClient(timeout=_PUSH_TIMEOUT, transport=self._transport, trust_env=False) as client:
                resp = await client.post(self.url, json=body, headers=headers)
            if resp.status_code >= 400:
                logger.warning(f"push webhook 非成功状态: {kind} status={resp.status_code}")
                return False
            logger.info(f"push webhook 已送达: {kind} status={resp.status_code}")
            return True
        except Exception as e:
            logger.warning(f"push webhook 失败 (忽略, 不阻塞): {kind} {e}")
            return False
