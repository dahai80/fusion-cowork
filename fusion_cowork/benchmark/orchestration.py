"""Orchestration benchmark (issue #466, W8) — executable orchestration eval in CI.

自建可执行编排评测: 一组确定性场景工作流 (无 LLM 依赖, 离线跑), 经真实
WorkflowEngine 执行后断言编排语义 (波次调度/数据传递/类型强转/重试/过滤/
子工作流嵌套)。CI 与 fusion-bench 并行运行, score=100% 为门禁。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_PROBES_REGISTERED = False


def _ensure_probe_nodes() -> None:
    # 评测探针节点 — 幂等注册 (重复导入/多次 run 不覆盖报错)
    global _PROBES_REGISTERED
    if _PROBES_REGISTERED:
        return
    from ..nodes import import_all_nodes

    import_all_nodes()
    from ..engine.node import BaseNode, NodeCategory, NodeRegistry, NodeResult, NodeStatus, register_node

    if NodeRegistry.get("bench_echo") is None:

        @register_node
        class BenchEcho(BaseNode):
            name = "bench_echo"
            display_name = "Bench Echo"
            category = NodeCategory.LOGIC
            description = "orchestration benchmark probe: passes seed + optional suffix through"
            icon = "🏁"
            default_label = "BenchEcho"

            async def execute(self, inputs: dict) -> NodeResult:
                seed = str(inputs.get("seed", ""))
                return NodeResult(status=NodeStatus.SUCCESS, data={"seed": seed}, summary="echo")

    if NodeRegistry.get("bench_count") is None:

        @register_node
        class BenchCount(BaseNode):
            name = "bench_count"
            display_name = "Bench Count"
            category = NodeCategory.LOGIC
            description = "orchestration benchmark probe: counts up to declared n (int coercion)"
            icon = "🏁"
            default_label = "BenchCount"

            def get_params_schema(self) -> dict:
                return {
                    "type": "object",
                    "properties": {"n": {"type": "integer", "description": "count target"}},
                    "required": ["n"],
                }

            async def execute(self, inputs: dict) -> NodeResult:
                n = int(inputs.get("n", 0))
                return NodeResult(status=NodeStatus.SUCCESS, data={"count": n}, summary="count")

    if NodeRegistry.get("bench_flaky") is None:

        @register_node
        class BenchFlaky(BaseNode):
            name = "bench_flaky"
            display_name = "Bench Flaky"
            category = NodeCategory.LOGIC
            description = "orchestration benchmark probe: fails first call, succeeds on retry"
            icon = "🏁"
            default_label = "BenchFlaky"

            calls = 0

            async def execute(self, inputs: dict) -> NodeResult:
                type(self).calls += 1
                if type(self).calls == 1:
                    return NodeResult(
                        status=NodeStatus.FAILED, error="bench_flaky first-attempt failure", summary="fail"
                    )
                return NodeResult(status=NodeStatus.SUCCESS, data={"attempts": type(self).calls}, summary="ok")

    if NodeRegistry.get("bench_gate") is None:

        @register_node
        class BenchGate(BaseNode):
            name = "bench_gate"
            display_name = "Bench Gate"
            category = NodeCategory.LOGIC
            description = "orchestration benchmark probe: emits marker, waits for directive flag"
            icon = "🏁"
            default_label = "BenchGate"

            flag = {"queued": False}

            async def execute(self, inputs: dict) -> NodeResult:
                for _ in range(250):
                    if type(self).flag.get("queued"):
                        break
                    await asyncio.sleep(0.02)
                return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="gate")

    if NodeRegistry.get("bench_slow") is None:

        @register_node
        class BenchSlow(BaseNode):
            name = "bench_slow"
            display_name = "Bench Slow"
            category = NodeCategory.LOGIC
            description = "orchestration benchmark probe: small deterministic sleep"
            icon = "🏁"
            default_label = "BenchSlow"

            async def execute(self, inputs: dict) -> NodeResult:
                await asyncio.sleep(0.1)
                return NodeResult(status=NodeStatus.SUCCESS, data={"done": True}, summary="slow")

    _PROBES_REGISTERED = True
    logger.info(
        "orchestration benchmark probe nodes registered (bench_echo/bench_count/bench_flaky/bench_gate/bench_slow)"
    )


def _wf(def_dict: Dict[str, Any]) -> Any:
    from ..engine.workflow import Workflow

    return Workflow.from_dict(def_dict)


def _linear() -> Tuple[Dict[str, Any], Callable[[Any], Tuple[bool, str]]]:
    d = {
        "name": "bench-linear",
        "nodes": [
            {"id": "a", "name": "bench_echo", "config": {}},
            {"id": "b", "name": "bench_echo", "config": {}},
            {"id": "c", "name": "bench_echo", "config": {}},
        ],
        "edges": [
            {"source_id": "a", "target_id": "b"},
            {"source_id": "b", "target_id": "c"},
        ],
    }

    def check(execution) -> Tuple[bool, str]:
        ok_steps = [s for s in execution.steps if s.status.value == "success"]
        if execution.status.value != "success":
            return False, f"终态 {execution.status.value}: {execution.error}"
        if len(ok_steps) != 3:
            return False, f"成功步骤 {len(ok_steps)} != 3"
        return True, "3 节点线性链全成功"

    return d, check


def _diamond() -> Tuple[Dict[str, Any], Callable[[Any], Tuple[bool, str]]]:
    d = {
        "name": "bench-diamond",
        "nodes": [
            {"id": "a", "name": "bench_echo", "config": {}},
            {"id": "b", "name": "bench_echo", "config": {}},
            {"id": "c", "name": "bench_echo", "config": {}},
            {"id": "d", "name": "bench_echo", "config": {}},
        ],
        "edges": [
            {"source_id": "a", "target_id": "b"},
            {"source_id": "a", "target_id": "c"},
            {"source_id": "b", "target_id": "d"},
            {"source_id": "c", "target_id": "d"},
        ],
    }

    def check(execution) -> Tuple[bool, str]:
        if execution.status.value != "success":
            return False, f"终态 {execution.status.value}: {execution.error}"
        d_steps = [s for s in execution.steps if s.node_id == "d" and s.status.value == "success"]
        if len(d_steps) != 1:
            return False, f"汇点 d 执行 {len(d_steps)} 次 (期待 1)"
        return True, "菱形 fan-out/fan-in 汇点单次执行"

    return d, check


def _retry() -> Tuple[Dict[str, Any], Callable[[Any], Tuple[bool, str]]]:
    BenchFlaky_reset()
    d = {
        "name": "bench-retry",
        "nodes": [
            {
                "id": "f",
                "name": "bench_flaky",
                "config": {"max_retries": 2, "retry_delay": 0.01, "retry_on_business_failure": True},
            },
        ],
        "edges": [],
    }

    def check(execution) -> Tuple[bool, str]:
        if execution.status.value != "success":
            return False, f"终态 {execution.status.value}: {execution.error}"
        return True, "首次失败重试后成功"

    return d, check


def BenchFlaky_reset() -> None:
    from ..engine.node import NodeRegistry

    node_cls = NodeRegistry._registry.get("bench_flaky")
    if node_cls is not None:
        node_cls.calls = 0


def _parallel() -> Tuple[Dict[str, Any], Callable[[Any], Tuple[bool, str]]]:
    d = {
        "name": "bench-parallel",
        "nodes": [
            {"id": "p1", "name": "bench_slow", "config": {}},
            {"id": "p2", "name": "bench_slow", "config": {}},
            {"id": "p3", "name": "bench_slow", "config": {}},
        ],
        "edges": [],
    }

    def check(execution) -> Tuple[bool, str]:
        if execution.status.value != "success":
            return False, f"终态 {execution.status.value}: {execution.error}"
        if execution.concurrency < 2:
            return False, f"并发未生效: concurrency={execution.concurrency}"
        if execution.total_time >= 0.3:
            return False, f"同波串行执行: {execution.total_time:.2f}s >= 0.3s"
        return True, f"3 节点同波并行 (concurrency={execution.concurrency}, {execution.total_time:.2f}s)"

    return d, check


def _subworkflow() -> Tuple[Dict[str, Any], Callable[[Any], Tuple[bool, str]]]:
    d = {
        "name": "bench-subworkflow",
        "nodes": [
            {
                "id": "sub",
                "name": "sub_workflow",
                "config": {
                    "params": {
                        "workflow": {
                            "nodes": [{"id": "c1", "name": "bench_echo", "config": {}}],
                            "edges": [],
                        },
                        "input": {"seed": "bench"},
                    }
                },
            }
        ],
        "edges": [],
    }

    def check(execution) -> Tuple[bool, str]:
        if execution.status.value != "success":
            return False, f"终态 {execution.status.value}: {execution.error}"
        out = execution.steps[0].output_data or {}
        if out.get("outputs", {}).get("seed") != "bench":
            return False, f"子工作流参数继承失败: {out.get('outputs')}"
        return True, "子工作流嵌套执行 + 参数继承"

    return d, check


class OrchestrationBenchmark:
    """可执行编排评测 — 场景集经真实引擎执行, 输出通过率/得分。"""

    def __init__(self, permission_manager: Optional[Any] = None):
        _ensure_probe_nodes()
        self._permission_manager = permission_manager

    def _engine(self) -> Any:
        from ..engine import PermissionLevel, PermissionManager

        pm = self._permission_manager
        if pm is None:
            pm = PermissionManager(level=PermissionLevel.BYPASS)
            # BYPASS 兜底再显式放行探针 + 用到的内置节点
            for tool in ("bench_echo", "bench_count", "bench_flaky", "bench_gate", "bench_slow", "sub_workflow"):
                pm.approve(tool)
        from ..engine.workflow import WorkflowEngine

        return WorkflowEngine(permission_manager=pm)

    async def run_async(self, scenarios: Optional[List[str]] = None) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        engine = self._engine()

        async def run_case(
            name: str,
            wf_def: Dict[str, Any],
            check: Callable[[Any], Tuple[bool, str]],
            pre: Optional[Callable[[], None]] = None,
            engine_kwargs: Optional[Dict[str, Any]] = None,
        ) -> None:
            t0 = time.time()
            try:
                if pre:
                    pre()
                execution = await asyncio.wait_for(engine.execute(_wf(wf_def), **(engine_kwargs or {})), timeout=60)
                ok, detail = check(execution)
            except Exception as e:
                ok, detail = False, f"场景异常: {e}"
            results.append({"name": name, "ok": ok, "detail": detail, "duration": round(time.time() - t0, 3)})
            if ok:
                logger.info(f"orch-bench PASS: {name} ({detail})")
            else:
                logger.warning(f"orch-bench FAIL: {name} ({detail})")

        # 类型强转场景不走引擎 (测 NodeRegistry.create 强转路径)
        async def run_coercion() -> None:
            from ..engine.node import NodeConfig, NodeRegistry

            t0 = time.time()
            try:
                node = NodeRegistry.create("bench_count", config=NodeConfig(params={"n": "7"}))
                coerced = (node.config.params or {}).get("n")
                res = await node.execute(dict(node.config.params or {}))
                ok = (
                    isinstance(coerced, int)
                    and coerced == 7
                    and res.status.value == "success"
                    and res.data.get("count") == 7
                )
                detail = "字符串参数强转为 int" if ok else f"coerced={coerced!r} count={res.data.get('count')!r}"
            except Exception as e:
                ok, detail = False, f"场景异常: {e}"
            results.append(
                {"name": "type_coercion", "ok": ok, "detail": detail, "duration": round(time.time() - t0, 3)}
            )

        for name, wf_def, check, ekw in (
            ("linear_chain", *_linear(), None),
            ("diamond_fanout", *_diamond(), None),
            ("retry_on_failure", *_retry(), None),
            ("wave_parallel", *_parallel(), {"concurrency": 3}),
            ("sub_workflow_nesting", *_subworkflow(), None),
        ):
            if scenarios and name not in scenarios:
                continue
            await run_case(name, wf_def, check, engine_kwargs=ekw)
        if not scenarios or "type_coercion" in scenarios:
            await run_coercion()
        passed = sum(1 for r in results if r["ok"])
        failed = len(results) - passed
        score = round(100.0 * passed / len(results), 1) if results else 0.0
        summary = {
            "benchmark": "orchestration",
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "score": score,
            "results": results,
        }
        logger.info(f"orchestration benchmark: {passed}/{len(results)} passed, score={score}")
        return summary

    def run(self, scenarios: Optional[List[str]] = None) -> Dict[str, Any]:
        return asyncio.run(self.run_async(scenarios))
