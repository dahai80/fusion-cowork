# issue #470: bind DAG node state to fusion-harness kernel primitives.
#
# KernelCheckpointBinder wraps two harness kernel stores:
#   - CheckpointStore: durable pre-execution state per DAG node
#     (session_id = run_id, one checkpoint per node, metadata carries
#     node_id/node_name/params so the latest checkpoint identifies
#     where a failed workflow stopped).
#   - ReceiptCache: committed-node receipt keyed by {run_id}:{node_id}.
#     A re-dispatched run with the same execution_id (zombie resume,
#     run-history restart) replays the stored output instead of
#     re-executing committed effects — idempotent by construction.
#
# Opt-in: WorkflowEngine(kernel_checkpoints=KernelCheckpointBinder(...)).
# Default None = zero behavior change.

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_RECEIPT_AGG = "cowork.node"
_MAX_SNAPSHOT_CHARS = 65536


class _ReceiptMissing(Exception):
    pass


def _raise_missing() -> Any:
    raise _ReceiptMissing()


def _json_safe(value: Any) -> Any:
    # ReceiptCache stores json.dumps(result) with no default= — round-trip
    # here so datetime/set artifacts in node output cannot break recording
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class KernelCheckpointBinder:
    """Bridge WorkflowEngine node lifecycle to harness CheckpointStore + ReceiptCache."""

    def __init__(self, db_path: str = ":memory:", clock: Optional[Callable[[], float]] = None):
        try:
            from fusion_harness import CheckpointStore, ReceiptCache
        except ImportError as e:
            raise ImportError(
                "KernelCheckpointBinder requires fusion-harness "
                "(pip install 'fusion-cowork[plugins]') — kernel primitives "
                "CheckpointStore/ReceiptCache missing"
            ) from e
        kwargs: Dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        self._store = CheckpointStore(path=db_path, **kwargs)
        self._receipts = ReceiptCache(db_path=db_path, **kwargs)
        logger.info("kernel checkpoint binder ready db=%s", db_path)

    @staticmethod
    def _receipt_key(run_id: str, node_id: str) -> str:
        return f"{run_id}:{node_id}"

    def save_node_checkpoint(
        self,
        run_id: str,
        node_id: str,
        node_name: str,
        params: Dict[str, Any],
        node_input: Dict[str, Any],
    ) -> int:
        from fusion_harness import CheckpointState

        state = CheckpointState(
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(node_input or {}, ensure_ascii=False, default=str)[:_MAX_SNAPSHOT_CHARS],
                }
            ],
            permission_mode="workflow",
            metadata={
                "node_id": node_id,
                "node_name": node_name,
                "params": _json_safe(params or {}),
            },
        )
        seq = self._store.save(run_id, state)
        logger.info("kernel checkpoint saved run=%s node=%s seq=%s", run_id, node_id, seq)
        return seq

    def record_receipt(self, run_id: str, node_id: str, output: Dict[str, Any]) -> Tuple[Any, bool]:
        # first() records on miss, replays on hit — recording path uses a
        # constant fn so a duplicate record call replays instead of failing
        payload = _json_safe(output or {})
        result, replayed = self._receipts.first(self._receipt_key(run_id, node_id), _RECEIPT_AGG, lambda: payload)
        logger.info("kernel receipt %s run=%s node=%s", "replayed" if replayed else "recorded", run_id, node_id)
        return result, replayed

    def replay_receipt(self, run_id: str, node_id: str) -> Optional[Any]:
        # has() probe then first() with a raising fn — first() would otherwise
        # record the probe miss as a receipt (its fn result is persisted)
        key = self._receipt_key(run_id, node_id)
        if not self._receipts.has(key):
            return None
        try:
            result, _replayed = self._receipts.first(key, _RECEIPT_AGG, _raise_missing)
        except _ReceiptMissing:
            return None
        return result

    def has_receipt(self, run_id: str, node_id: str) -> bool:
        return self._receipts.has(self._receipt_key(run_id, node_id))

    def latest_checkpoint(self, run_id: str) -> Optional[Any]:
        return self._store.latest(run_id)

    def list_checkpoints(self, run_id: str) -> List[Dict[str, Any]]:
        return self._store.list_checkpoints(run_id)

    def close(self) -> None:
        self._store._conn.close()
        self._receipts.close()
