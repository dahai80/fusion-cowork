"""Transactional outbox integration — fusion-harness kernel (harness#11) wired into the workflow engine (cowork#382).

Semantics (t3code pattern, Python kernel in fusion_harness.outbox):
- engine journals node-result events + enqueues side effects in ONE sqlite
  transaction (OutboxStore.commit_step) — a crash between state change and
  side effect loses nothing
- GitHub write ops run through idempotent command receipts
  (OutboxStore.record_command) — a replayed (run, node, op, key) is a no-op
  returning the recorded result, so retries never duplicate writes
- effect worker claims/sets with persisted backoff (available_at), never
  in-memory retry timers
- boot reconcile requeues expired leases and retires orphaned-run effects;
  callers mark orphaned runs' honest terminal states (#372 recover_persisted)

Opt-in/out: enabled when fusion_harness imports; FUSION_COWORK_OUTBOX=0
disables. Kernel missing = disabled + one loud warning, zero behavior change.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

try:
    from fusion_harness import OutboxStore

    _KERNEL_IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - import guard
    OutboxStore = None  # type: ignore[assignment,misc]
    _KERNEL_IMPORT_ERROR = str(e)

# node execution context for side-effect receipt keying — set by WorkflowEngine
# around each node execution, read by nodes routing writes through the receipt
# layer (issue #382)
outbox_ctx: contextvars.ContextVar = contextvars.ContextVar("cowork_outbox_ctx", default=None)

_BACKOFF_BASE = 30.0
_BACKOFF_CAP = 600.0

_EFFECT_HANDLERS: dict[str, Callable[..., Any]] = {}


def register_effect_handler(action: str, fn: Callable[..., Any]) -> None:
    # worker dispatch table — handlers receive (effect, payload) and raise on failure
    _EFFECT_HANDLERS[str(action)] = fn


def backoff_seconds(attempts: int) -> float:
    return min(_BACKOFF_BASE * (2 ** max(0, attempts - 1)), _BACKOFF_CAP)


class OutboxBridge:
    """Thin cowork-side facade over the fusion-harness OutboxStore.

    The kernel sqlite connection is bound to its creating thread
    (check_same_thread) while callers span threads (serve startup, engine
    loop, worker loop) — the bridge keeps one store PER THREAD over the same
    db file (WAL, multi-conn safe).
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()
        self._worker_task: asyncio.Task | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None

    @property
    def enabled(self) -> bool:
        return OutboxStore is not None

    @property
    def _store(self):
        # per-thread store instance — lazily opened on first use in this thread
        store = getattr(self._local, "store", None)
        if store is None:
            store = OutboxStore(self._db_path)
            self._local.store = store
        return store

    def record_step(self, run_id: str, node_id: str, status: str, summary: str, effects: list[dict]) -> None:
        # journal the node result + enqueue declared effects in one transaction;
        # journaling must never break the run — failures are loud, not fatal
        if not self.enabled:
            return
        try:
            self._store.commit_step(
                run_id,
                [{"type": "node_result", "payload": {"node_id": node_id, "status": status, "summary": summary}}],
                effects or [],
            )
        except Exception as e:
            logger.warning("outbox record_step failed run=%s node=%s: %s", run_id, node_id, e)

    def execute_idempotent(self, command_id: str, aggregate: str, fn: Callable[..., Any], *args, **kwargs):
        # replayed command_id returns the recorded result without re-running fn
        result, replayed = self._store.record_command(command_id, aggregate, fn, *args, **kwargs)
        if replayed:
            logger.info("outbox receipt replayed id=%s (write deduped)", command_id)
        return result, replayed

    async def execute_idempotent_async(self, command_id: str, aggregate: str, run_fn: Callable[[], Any]):
        # async write under a receipt: pre-check has_receipt, run, then record.
        # Crash window between write and receipt-record is narrowed, not
        # eliminated (kernel receipt store is sync sqlite; full transactional
        # coverage would need the effects path) — downstream search-dedupe
        # (issue_create #352/#368) stays as the second net.
        if not self.enabled or not command_id:
            return await run_fn(), False
        if self._store.has_receipt(command_id):
            result, _ = self._store.record_command(command_id, aggregate, lambda: None)
            logger.info("outbox write receipt replayed id=%s (write skipped)", command_id)
            return result, True
        result = await run_fn()
        try:
            self._store.record_command(command_id, aggregate, lambda: result)
        except Exception as e:
            logger.warning("outbox receipt record failed id=%s: %s", command_id, e)
        return result, False

    def reconcile(self, orphaned_run_ids: list[str] | None = None):
        if not self.enabled:
            return None
        report = self._store.reconcile(orphaned_run_ids)
        if orphaned_run_ids:
            logger.warning("outbox boot reconcile: %s (orphaned runs retired honestly)", report)
        return report

    def ensure_worker(self) -> None:
        # one worker per event loop — effects claim/settle serially (at-least-once)
        if not self.enabled or self._worker_task is not None:
            return
        self._worker_loop = asyncio.get_running_loop()
        self._worker_task = self._worker_loop.create_task(self._worker())
        logger.info("outbox effect worker started")

    async def stop_worker(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

    async def _worker(self) -> None:
        # store calls run on the loop thread on purpose: the kernel sqlite
        # connection is bound to its creating thread (check_same_thread) —
        # to_thread would raise ProgrammingError on every claim
        while True:
            effect = self._store.claim_next()
            if effect is None:
                await asyncio.sleep(2.0)
                continue
            handler = _EFFECT_HANDLERS.get(effect.action)
            if handler is None:
                logger.warning("outbox effect id=%s action=%s has no handler, retiring", effect.id, effect.action)
                self._store.settle(effect.id, False, "no handler registered", _BACKOFF_CAP)
                continue
            try:
                await handler(effect, effect.payload)
            except Exception as e:
                logger.warning("outbox effect id=%s action=%s failed: %s", effect.id, effect.action, e)
                self._store.settle(effect.id, False, str(e), backoff_seconds(effect.attempts))
            else:
                self._store.settle(effect.id, True)


_BRIDGE: OutboxBridge | None = None
_BRIDGE_LOCK = threading.Lock()
_WARNED = False


def get_outbox_bridge() -> OutboxBridge | None:
    # singleton; disabled when kernel missing (one loud warning) or opt-out env
    global _BRIDGE, _WARNED
    if os.environ.get("FUSION_COWORK_OUTBOX", "").strip().lower() in ("0", "false", "off", "no"):
        return None
    with _BRIDGE_LOCK:
        if _BRIDGE is not None:
            return _BRIDGE
        if OutboxStore is None:
            if not _WARNED:
                _WARNED = True
                logger.warning(
                    "fusion-harness outbox kernel unavailable (FUSION_COWORK_OUTBOX disabled): %s — "
                    "side-effect receipts/journal off, runs proceed without outbox",
                    _KERNEL_IMPORT_ERROR,
                )
            return None
        db_path = os.environ.get("FUSION_COWORK_OUTBOX_DB") or os.path.expanduser("~/.fusion-cowork/outbox/outbox.db")
        _BRIDGE = OutboxBridge(db_path)
        logger.info("outbox bridge enabled db=%s", db_path)
        return _BRIDGE


def reset_outbox_bridge() -> None:
    # test seam — drop singleton + worker state
    global _BRIDGE, _WARNED
    with _BRIDGE_LOCK:
        _BRIDGE = None
        _WARNED = False
