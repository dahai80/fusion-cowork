"""推送出口 — 任务完成 / 审批请求外发 (issue #154)。

PushEgress: webhook egress, best-effort fire-and-forget:
- 未配 FUSION_COWORK_PUSH_WEBHOOK_URL → 空实现 (零行为变化)
- 配置后: run 终态 + 审批入队事件 POST 到 webhook, 失败仅记日志 (不阻塞主流程)
- 密钥: FUSION_COWORK_PUSH_WEBHOOK_SECRET → X-Fusion-Push-Secret 头

ReliableEgress (issue #579): at-least-once 完成通知 — 持久 outbox:
- persist-before-ack: 事件先落盘再发起投递, 崩溃不丢已登记事件
- 投递失败重试 (flush_pending 直到 2xx), 崩溃窗口 (已送达未标记) 可能重复 —
  接收方按事件 id 幂等去重, 此处如实重复投递
- FUSION_COWORK_PUSH_AT_LEAST_ONCE=1 且 webhook URL 已配时启用, 默认关
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

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


def _default_outbox_path() -> str:
    base = os.environ.get("FUSION_COWORK_PUSH_OUTBOX_PATH", "").strip()
    if base:
        return base
    return os.path.join(os.path.expanduser("~"), ".fusion-cowork", "web", "push_outbox.json")


class ReliableEgress:
    """at-least-once 完成通知 — persist-before-ack 持久 outbox (issue #579)。

    事件登记 (落盘) 先于首次投递; flush_pending 对每条 pending 投递直到 2xx,
    成功才移除。崩溃窗口: 投递成功但移除未发生 → 重启后重投 (重复, 如实文档化)。
    """

    def __init__(self, egress: PushEgress, path: str = ""):
        self.egress = egress
        self.path = path or _default_outbox_path()
        self._pending: List[Dict[str, Any]] = []
        self._loaded = False

    @classmethod
    def from_env(cls) -> Optional[ReliableEgress]:
        # env 未开 (或 webhook URL 未配) → None, 调用方走原 best-effort 路径
        flag = os.environ.get("FUSION_COWORK_PUSH_AT_LEAST_ONCE", "").strip().lower()
        if flag not in ("1", "true", "yes", "on"):
            return None
        egress = PushEgress.from_env()
        if not egress.enabled:
            logger.warning("at-least-once push 开启但 webhook URL 未配, 回退 best-effort")
            return None
        return cls(egress, path=_default_outbox_path())

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._pending = [r for r in data if isinstance(r, dict) and r.get("kind")]
        except FileNotFoundError:
            self._pending = []
        except Exception as e:
            logger.warning(f"push outbox 读取失败 (按空处理): {self.path} {e}")
            self._pending = []

    def _save(self) -> None:
        # 原子写 — temp + os.replace, 避免半行损坏
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._pending, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def enqueue(self, kind: str, payload: Dict[str, Any]) -> bool:
        # persist-before-ack — 登记落盘先于任何投递尝试
        self._load()
        record = {
            "id": f"pe_{uuid.uuid4().hex[:12]}",
            "kind": kind,
            "payload": dict(payload),
            "ts": time.time(),
            "attempts": 0,
        }
        self._pending.append(record)
        try:
            self._save()
            logger.info(f"push outbox 已登记: id={record['id']} kind={kind} pending={len(self._pending)}")
            return True
        except Exception as e:
            logger.error(f"push outbox 落盘失败 (事件未登记): {kind} {e}")
            self._pending.pop()
            return False

    def pending_count(self) -> int:
        self._load()
        return len(self._pending)

    async def flush_pending(self) -> int:
        # 逐条投递, 2xx 才移除; 失败保留 (attempts+1) 等下次 flush/重启
        self._load()
        delivered = 0
        changed = False
        remaining: List[Dict[str, Any]] = []
        for record in self._pending:
            ok = await self.egress.push_event(record["kind"], record["payload"])
            if ok:
                delivered += 1
                changed = True
            else:
                record["attempts"] = int(record.get("attempts", 0)) + 1
                remaining.append(record)
                changed = True
                logger.warning(f"push outbox 投递失败保留重试: id={record['id']} attempts={record['attempts']}")
        if changed:
            self._pending = remaining
            try:
                self._save()
            except Exception as e:
                logger.error(f"push outbox 回写失败 (重启后可能重复投递): {e}")
        return delivered
