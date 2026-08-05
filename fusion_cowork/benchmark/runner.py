"""BenchmarkRunner — 执行节点/工作流计时，产出 BenchmarkResult。"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkResult:
    node_name: str
    status: str
    latency_ms: float
    data: Any = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_name": self.node_name,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
        }


class BenchmarkRunner:
    """基准执行器 — 逐个执行节点并计时。"""

    def __init__(self, warmup: int = 1, repeats: int = 3):
        self._warmup = warmup
        self._repeats = repeats
        self._results: List[BenchmarkResult] = []

    async def run_node(self, node_name: str, params: Dict[str, Any]) -> BenchmarkResult:
        from ..engine.node import NodeConfig, NodeRegistry

        node = NodeRegistry.create(node_name, config=NodeConfig(params=params))
        if not node:
            logger.warning(f"节点未注册: {node_name}")
            return BenchmarkResult(node_name=node_name, status="not_found", latency_ms=0)

        t0 = time.monotonic()
        try:
            result = await node.execute(params)
            elapsed = (time.monotonic() - t0) * 1000
            br = BenchmarkResult(
                node_name=node_name,
                status=result.status.value,
                latency_ms=elapsed,
                data=result.data,
                error=result.error or "",
            )
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            br = BenchmarkResult(
                node_name=node_name,
                status="error",
                latency_ms=elapsed,
                error=str(e),
            )
        self._results.append(br)
        logger.info(f"Benchmark {node_name}: {br.latency_ms:.1f}ms status={br.status}")
        return br

    async def run_nodes(self, specs: List[Dict[str, Any]]) -> List[BenchmarkResult]:
        results = []
        for spec in specs:
            node_name = spec.get("node", "")
            params = spec.get("params", {})
            for i in range(self._warmup):
                await self.run_node(node_name, params)
            for i in range(self._repeats):
                br = await self.run_node(node_name, params)
                results.append(br)
        return results

    async def run_workflow(self, workflow_dict: Dict[str, Any]) -> BenchmarkResult:
        from ..engine.workflow import Workflow, WorkflowEngine

        wf = Workflow.from_dict(workflow_dict)
        engine = WorkflowEngine()

        t0 = time.monotonic()
        try:
            result = await engine.execute(wf)
            elapsed = (time.monotonic() - t0) * 1000
            br = BenchmarkResult(
                node_name=wf.name or "workflow",
                status=result.status.value,
                latency_ms=elapsed,
                data=result.data,
            )
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            br = BenchmarkResult(
                node_name=wf.name or "workflow",
                status="error",
                latency_ms=elapsed,
                error=str(e),
            )
        self._results.append(br)
        logger.info(f"Benchmark workflow: {br.latency_ms:.1f}ms status={br.status}")
        return br

    def results(self) -> List[BenchmarkResult]:
        return list(self._results)

    def summary(self) -> Dict[str, Any]:
        if not self._results:
            return {"total": 0}
        by_node: Dict[str, List[float]] = {}
        for r in self._results:
            by_node.setdefault(r.node_name, []).append(r.latency_ms)
        stats = {}
        for name, latencies in by_node.items():
            avg = sum(latencies) / len(latencies)
            mn = min(latencies)
            mx = max(latencies)
            stats[name] = {
                "avg_ms": round(avg, 2),
                "min_ms": round(mn, 2),
                "max_ms": round(mx, 2),
                "runs": len(latencies),
            }
        return {"total_runs": len(self._results), "nodes": stats}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "summary": self.summary(),
                "results": [r.to_dict() for r in self._results],
            },
            indent=indent,
            ensure_ascii=False,
        )
