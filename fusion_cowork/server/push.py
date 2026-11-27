"""Push egress — outbound delivery of task completions / approval requests (issue #154).

PushEgress: webhook egress, best-effort fire-and-forget:
- FUSION_COWORK_PUSH_WEBHOOK_URL unset → no-op (zero behavior change)
- when configured: run terminal states + approval enqueued events POST to the webhook; failures are only logged (never block the main flow)
- secret: FUSION_COWORK_PUSH_WEBHOOK_SECRET → X-Fusion-Push-Secret header

ReliableEgress (issue #579): at-least-once completion notifications — persistent outbox:
- persist-before-ack: events are written to disk before delivery is attempted, no registered event is lost on crash
- delivery retries on failure (flush_pending until 2xx); the crash window (delivered but unmarked) may duplicate —
  receivers dedupe idempotently by event id, this side re-delivers honestly
- enabled when FUSION_COWORK_PUSH_AT_LEAST_ONCE=1 and the webhook URL is configured, default off
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
        # transport: httpx.AsyncClient-compatible injection (test MockTransport); None → built in production
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
        """Send one event. Returns whether it was actually delivered (False on disabled/failed, never raises)."""
        if not self.enabled:
            logger.debug(f"push not enabled, dropping event: {kind}")
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
                logger.warning(f"push webhook non-success status: {kind} status={resp.status_code}")
                return False
            logger.info(f"push webhook delivered: {kind} status={resp.status_code}")
            return True
        except Exception as e:
            logger.warning(f"push webhook failed (ignored, non-blocking): {kind} {e}")
            return False


def _default_outbox_path() -> str:
    base = os.environ.get("FUSION_COWORK_PUSH_OUTBOX_PATH", "").strip()
    if base:
        return base
    return os.path.join(os.path.expanduser("~"), ".fusion-cowork", "web", "push_outbox.json")


class ReliableEgress:
    """at-least-once completion notifications — persist-before-ack durable outbox (issue #579).

    Event registration (disk write) happens before the first delivery; flush_pending delivers each
    pending record until 2xx and only then removes it. Crash window: delivery succeeded but removal
    did not happen → redelivered after restart (duplicate, documented honestly).
    """

    def __init__(self, egress: PushEgress, path: str = ""):
        self.egress = egress
        self.path = path or _default_outbox_path()
        self._pending: List[Dict[str, Any]] = []
        self._loaded = False

    @classmethod
    def from_env(cls) -> Optional[ReliableEgress]:
        # env flag off (or webhook URL not configured) → None, the caller falls back to the original best-effort path
        flag = os.environ.get("FUSION_COWORK_PUSH_AT_LEAST_ONCE", "").strip().lower()
        if flag not in ("1", "true", "yes", "on"):
            return None
        egress = PushEgress.from_env()
        if not egress.enabled:
            logger.warning("at-least-once push enabled but webhook URL not configured, falling back to best-effort")
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
            logger.warning(f"push outbox read failed (treated as empty): {self.path} {e}")
            self._pending = []

    def _save(self) -> None:
        # atomic write — temp + os.replace, avoids half-line corruption
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._pending, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def enqueue(self, kind: str, payload: Dict[str, Any]) -> bool:
        # persist-before-ack — registration is persisted before any delivery attempt
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
            logger.info(f"push outbox enqueued: id={record['id']} kind={kind} pending={len(self._pending)}")
            return True
        except Exception as e:
            logger.error(f"push outbox persist failed (event not registered): {kind} {e}")
            self._pending.pop()
            return False

    def pending_count(self) -> int:
        self._load()
        return len(self._pending)

    async def flush_pending(self) -> int:
        # deliver one by one, remove only on 2xx; failures are kept (attempts+1) for the next flush/restart
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
                logger.warning(
                    f"push outbox delivery failed, kept for retry: id={record['id']} attempts={record['attempts']}"
                )
        if changed:
            self._pending = remaining
            try:
                self._save()
            except Exception as e:
                logger.error(f"push outbox write-back failed (possible duplicate delivery after restart): {e}")
        return delivered
