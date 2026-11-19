"""issue #545: DAG run/node telemetry into fusion_ops.ops_events (flywheel Loop D foundation).

Thin adapter over fusion_core.OpsEmitter (async batched PG writer). Event
contract per architecture/fusion-flywheel-1119.md §2 + issue #545:

- run_started / run_finished: outcome + total duration per run
- node_started / node_finished: node type, duration_ms, attempt count,
  failure signature (error class + node type — stable mined_issues cluster key)
- Node inputs/outputs are NEVER emitted (may carry tenant code/data); only
  the sha256 of the workflow definition for version correlation.

Backpressure: OpsEmitter.emit never blocks (full queue = drop + counter),
so pipeline execution latency is unchanged within noise. Emitter is
optional — None (no FUSION_OPS_DSN) = zero-cost no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ops_events schema CHECK constraint (fusion-core ops.py) allows: cowork, gateway,
# identity, commerce, autotest, support, ops — "cowork" is our row
_SOURCE = "cowork"


def workflow_definition_hash(workflow: Any) -> str:
    # issue #545: definition hash only — version correlation without tenant data.
    # Canonical form EXCLUDES per-node instance ids (uuid): two identical
    # definitions must hash equal; structure = node types + edge topology.
    try:
        nodes = workflow.nodes.values() if isinstance(workflow.nodes, dict) else workflow.nodes
        id_to_name = {n.id: n.name for n in nodes}
        canonical = {
            "name": workflow.name,
            "nodes": sorted(id_to_name.values()),
            "edges": sorted(f"{id_to_name[e.source_id]}->{id_to_name[e.target_id]}" for e in workflow.edges),
        }
        raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"workflow definition hash failed (empty hash): {e}")
        return ""
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def failure_signature(node_type: str, error_class: str) -> str:
    # issue #545: stable cluster key — "node_type:ErrorClass" groups repeated
    # failures across runs for mined_issues; no message text (unstable, may
    # embed tenant paths)
    return f"{node_type}:{error_class or 'Unknown'}"


class OpsTelemetry:
    """Owns the optional OpsEmitter and formats cowork DAG events."""

    def __init__(self, emitter: Optional[Any] = None, source: str = _SOURCE):
        self._emitter = emitter
        self._source = source

    @classmethod
    def from_env(cls) -> Optional[OpsTelemetry]:
        # FUSION_OPS_DSN unset = telemetry off (zero behavior change); set =
        # OpsEmitter (asyncpg needed at flush, not here)
        dsn = os.environ.get("FUSION_OPS_DSN", "").strip()
        if not dsn:
            return None
        try:
            from fusion_core.ops import OpsEmitter
        except ImportError as e:
            logger.warning(f"FUSION_OPS_DSN set but fusion-core OpsEmitter unavailable (telemetry off): {e}")
            return None
        emitter = OpsEmitter(dsn, source=_SOURCE)
        logger.info("ops telemetry enabled (fusion_ops.ops_events via OpsEmitter)")
        return cls(emitter)

    async def start(self) -> None:
        if self._emitter is not None:
            try:
                await self._emitter.start()
            except Exception as e:
                logger.warning(f"ops telemetry start failed (events will drop): {e}")

    async def stop(self) -> None:
        if self._emitter is not None:
            try:
                await self._emitter.stop()
            except Exception as e:
                logger.warning(f"ops telemetry stop failed (ignored): {e}")

    @property
    def stats(self) -> Dict[str, int]:
        em = getattr(self._emitter, "stats", None)
        if em is None:
            return {}
        return {"emitted": em.emitted, "flushed": em.flushed, "dropped": em.dropped, "reconnects": em.reconnects}

    async def _emit(self, event: Dict[str, Any]) -> None:
        # never raise into the engine — telemetry must not affect runs
        if self._emitter is None:
            return
        try:
            await self._emitter.emit(event)
        except Exception as e:
            logger.debug(f"ops telemetry emit failed (ignored): {e}")

    async def run_started(self, run_id: str, workflow_hash: str, node_count: int, hitl_mode: str = "") -> None:
        await self._emit(
            {
                "event_type": "dag_run_started",
                "payload": {
                    "run_id": run_id,
                    "workflow_hash": workflow_hash,
                    "node_count": node_count,
                    "hitl_mode": hitl_mode,
                },
            }
        )

    async def run_finished(self, run_id: str, outcome: str, duration_ms: int, workflow_hash: str = "") -> None:
        await self._emit(
            {
                "event_type": "dag_run_finished",
                "latency_ms": max(0, int(duration_ms)),
                "status": outcome,
                "payload": {"run_id": run_id, "workflow_hash": workflow_hash},
            }
        )

    async def node_started(self, run_id: str, node_id: str, node_type: str) -> None:
        await self._emit(
            {
                "event_type": "dag_node_started",
                "payload": {"run_id": run_id, "node_id": node_id, "node_type": node_type},
            }
        )

    async def node_finished(
        self,
        run_id: str,
        node_id: str,
        node_type: str,
        status: str,
        duration_ms: int,
        attempts: int = 1,
        failure_sig: str = "",
    ) -> None:
        await self._emit(
            {
                "event_type": "dag_node_finished",
                "latency_ms": max(0, int(duration_ms)),
                "status": status,
                "payload": {
                    "run_id": run_id,
                    "node_id": node_id,
                    "node_type": node_type,
                    "attempts": attempts,
                    "failure_signature": failure_sig,
                },
            }
        )
