"""Mailbox agent service loop (issue #546, P0 observe-only).

Orchestrates poll -> dedupe/store -> classify -> route-record -> daily
digest. Opt-in via FUSION_MAILBOX_ENABLED=1; IMAP credentials from env
(fusion-sv T2 injection, never in repo). Crash-recoverable: last IMAP UID
persisted in poll_state; duplicate fetches deduped by message-id.

P0 red lines: zero outbound mail except the daily digest to the owner;
every message becomes a feedback row (spec §6).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any, Optional

from .classifier import MailClassifier
from .digest import DigestBuilder
from .poller import MailboxPoller, imap_config_from_env
from .store import FeedbackStore

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 300
_DIGEST_INTERVAL_H = 24


class MailboxAgent:
    def __init__(
        self,
        store: Optional[FeedbackStore] = None,
        poller: Optional[MailboxPoller] = None,
        classifier: Optional[MailClassifier] = None,
        digest: Optional[DigestBuilder] = None,
        poll_interval_s: int = _POLL_INTERVAL_S,
    ):
        self._store = store or FeedbackStore()
        self._poller = poller
        self._classifier = classifier
        self._digest = digest or DigestBuilder()
        self._poll_interval_s = poll_interval_s
        self._stopping = False
        self._task: Optional[asyncio.Task] = None
        self.stats = {"polled": 0, "fetched": 0, "classified": 0, "duplicates": 0, "errors": 0, "digests": 0}

    @classmethod
    def from_env(cls, mlx: Any = None, **kwargs: Any) -> Optional[MailboxAgent]:
        # None unless explicitly enabled AND IMAP configured — zero cost otherwise
        if os.environ.get("FUSION_MAILBOX_ENABLED", "") not in ("1", "true", "yes", "on"):
            return None
        config = imap_config_from_env()
        if config is None:
            logger.warning("FUSION_MAILBOX_ENABLED set but IMAP config incomplete (agent off)")
            return None
        poller = MailboxPoller(config)
        if mlx is None:
            from ..ai.mlx_client import FusionMLXClient

            mlx = FusionMLXClient()
        model = os.environ.get("FUSION_MAILBOX_MODEL", "").strip() or "qwen3.5-9b"
        classifier = MailClassifier(mlx, model=model)
        logger.info(f"mailbox agent enabled (folder={config['folder']}, model={model}, P0 observe-only)")
        return cls(poller=poller, classifier=classifier, **kwargs)

    async def process_once(self) -> int:
        # one poll cycle; returns number of NEW messages processed
        state_key = MailboxPoller.state_key()
        known_uid = await self._store.get_state(state_key)
        self.stats["polled"] += 1
        try:
            result = await self._poller.fetch_new(known_uid)
        except Exception as e:
            self.stats["errors"] += 1
            logger.warning(f"mailbox poll failed (retry next cycle): {e}")
            return 0
        new_count = 0
        for msg in result["messages"]:
            row_id = await self._store.record_message(msg)
            if row_id is None:
                self.stats["duplicates"] += 1
                continue
            new_count += 1
            self.stats["fetched"] += 1
            classification = await self._classifier.classify(msg)
            await self._store.record_classification(msg["message_id"], classification)
            self.stats["classified"] += 1
            logger.info(
                f"mailbox message classified: {msg['message_id'][:40]} category={classification['category']} "
                f"conf={classification['confidence']:.2f} human_queue={classification['human_queue']} "
                f"route={classification['routed_to']}"
            )
        if result["last_uid"] and result["last_uid"] != known_uid:
            await self._store.set_state(state_key, result["last_uid"])
        return new_count

    async def maybe_digest(self, last_digest: datetime) -> datetime:
        if (datetime.now(UTC) - last_digest).total_seconds() < _DIGEST_INTERVAL_H * 3600:
            return last_digest
        sent = await self._digest.build_and_send(self._store, window_hours=_DIGEST_INTERVAL_H)
        if sent:
            self.stats["digests"] += 1
        return datetime.now(UTC)

    async def run_forever(self) -> None:
        # crash-recoverable loop: poll errors logged + retried next cycle,
        # UID checkpoint advances only after a successful fetch
        last_digest = datetime.now(UTC)
        logger.info(f"mailbox agent loop started (interval={self._poll_interval_s}s, P0 observe-only)")
        while not self._stopping:
            try:
                await self.process_once()
                last_digest = await self.maybe_digest(last_digest)
            except Exception as e:
                self.stats["errors"] += 1
                logger.exception(f"mailbox agent cycle error (continue): {e}")
            await asyncio.sleep(self._poll_interval_s)

    async def start(self) -> None:
        if self._task is not None:
            logger.warning("mailbox agent already running")
            return
        await self._store.connect()
        self._stopping = False
        self._task = asyncio.create_task(self.run_forever(), name="fusion-cowork-mailbox-agent")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._store.close()
        logger.info(f"mailbox agent stopped stats={self.stats}")
