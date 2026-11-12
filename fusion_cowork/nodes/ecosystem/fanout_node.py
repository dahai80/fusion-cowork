"""Coordinator-subagent fan-out + session fork nodes (issue #467, phase 3.2/3.3).

coordinator_fanout: node-level coordinator/worker pattern on fusion-harness
SubagentRunner — each worker is an isolated child execution (own session id,
narrowed tool surface, capped budget, summary-only return), fanned out in one
wave slot and gathered here. Parity with Temporal multi_agent / OpenAI
handoffs, but rides wave scheduling + HITL gates (HIGH_RISK_NODES) + device
nodes that cloud frameworks lack.

session_fork: one parent session branches into parallel child branches, each
holding its own session id ({parent}:fork:{branch}); fork happens at a wave
boundary (the node occupies one wave slot), gather happens at this merge node.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from ...engine.node import BaseNode, NodeCategory, NodeConfig, NodeRegistry, NodeResult, NodeStatus, register_node

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 600.0
_DEFAULT_BUDGET = 50_000
_DEFAULT_MAX_TURNS = 20


def _status_str(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _parent_session_id() -> str:
    from ...observability.trace import get_trace_id

    return get_trace_id() or "local"


def _load_runner() -> Any:
    try:
        from fusion_harness import SubagentRunner
    except ImportError as e:
        raise ImportError(
            "coordinator_fanout requires fusion-harness (pip install 'fusion-cowork[harness]') "
            "— kernel primitive SubagentRunner missing"
        ) from e
    return SubagentRunner()


@register_node
class CoordinatorFanoutNode(BaseNode):
    """协调分发节点 — coordinator/worker 扇出 (fusion-harness 子代理原语)。"""

    name = "coordinator_fanout"
    display_name = "协调分发"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "Fan out workers as isolated harness subagents, gather summary-only results"
    icon = "🎯"
    default_label = "协调分发"

    inputs = [
        {"key": "workers", "label": "worker 规格列表", "type": "array"},
    ]
    outputs = [
        {"key": "results", "label": "worker 结果列表", "type": "array"},
        {"key": "total", "label": "worker 总数", "type": "number"},
        {"key": "failed", "label": "失败数", "type": "number"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workers": {
                    "type": "array",
                    "description": "worker 规格 [{name, prompt, tool_allowlist?, permission_mode?, "
                    "budget_tokens?, max_turns?, worker_node?, worker_params?}]",
                },
                "sequential": {"type": "boolean", "description": "顺序执行 (默认并行)", "default": False},
                "allow_partial": {"type": "boolean", "description": "部分失败不整体 FAILED", "default": False},
                "timeout": {"type": "number", "description": "扇出超时秒数 (0 = 不限)", "default": _DEFAULT_TIMEOUT},
            },
            "required": ["workers"],
        }

    async def execute(self, inputs: dict) -> NodeResult:
        params = getattr(self.config, "params", {}) or {}
        workers = params.get("workers") or inputs.get("workers")
        if not isinstance(workers, list) or not workers:
            return NodeResult(status=NodeStatus.FAILED, error="coordinator_fanout 参数 workers 必填 (非空数组)")
        try:
            runner = _load_runner()
        except ImportError as e:
            return NodeResult(status=NodeStatus.FAILED, error=str(e))

        async def run_worker(spec_dict: Dict[str, Any]) -> Dict[str, Any]:
            from fusion_harness import SubagentSpec

            name = str(spec_dict.get("name") or f"worker-{workers.index(spec_dict) + 1}")
            try:
                prompt = str(spec_dict.get("prompt") or "")
                if not prompt:
                    raise ValueError(f"worker '{name}' 缺 prompt")
                spec = SubagentSpec(
                    name=name,
                    prompt=prompt,
                    tool_allowlist=list(spec_dict.get("tool_allowlist") or []),
                    permission_mode=str(spec_dict.get("permission_mode") or "default"),
                    budget_tokens=int(spec_dict.get("budget_tokens") or _DEFAULT_BUDGET),
                    max_turns=int(spec_dict.get("max_turns") or _DEFAULT_MAX_TURNS),
                    metadata={
                        "worker_node": spec_dict.get("worker_node") or "",
                        "worker_params": dict(spec_dict.get("worker_params") or {}),
                    },
                )

                async def agent_fn(spec: Any, session: Any) -> str:
                    session.record_turn()
                    worker_node = spec.metadata.get("worker_node")
                    if worker_node:
                        node = NodeRegistry.create(
                            str(worker_node), config=NodeConfig(params=dict(spec.metadata.get("worker_params") or {}))
                        )
                        if node is None:
                            raise ValueError(f"worker '{spec.name}' 的 worker_node '{worker_node}' 未注册")
                        res = await node.execute(
                            {"prompt": spec.prompt, "worker": spec.name, "session_id": session.subagent_id}
                        )
                        if _status_str(res.status) != "success":
                            raise RuntimeError(f"worker_node '{worker_node}' 执行失败: {res.error or '无错误详情'}")
                        return str(res.summary or "worker_node 完成")
                    return f"worker {spec.name} acknowledged prompt ({len(spec.prompt)} chars)"

                result = await runner.run(spec, agent_fn, parent_tools=["coordinator_fanout"], parent_mode="default")
            except Exception as e:
                logger.warning(f"coordinator_fanout worker '{name}' failed: {e}")
                return {
                    "name": name,
                    "subagent_id": "",
                    "status": "failed",
                    "summary": "",
                    "turns": 0,
                    "tokens_spent": 0,
                    "error": str(e),
                }
            return {
                "name": result.name,
                "subagent_id": result.subagent_id,
                "status": result.status,
                "summary": result.summary,
                "turns": result.turns,
                "tokens_spent": result.tokens_spent,
                "error": result.error,
            }

        sequential = bool(params.get("sequential", False))
        try:
            timeout = float(params.get("timeout", _DEFAULT_TIMEOUT) or 0)
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        logger.info(f"coordinator_fanout 开始: workers={len(workers)} sequential={sequential}")
        try:
            if sequential:
                raw = [await run_worker(w) for w in workers]
            elif timeout > 0:
                raw = await asyncio.wait_for(asyncio.gather(*(run_worker(w) for w in workers)), timeout=timeout)
            else:
                raw = list(await asyncio.gather(*(run_worker(w) for w in workers)))
        except TimeoutError:
            return NodeResult(status=NodeStatus.FAILED, error=f"coordinator_fanout 扇出超时 ({timeout:g}s)")
        except ImportError as e:
            return NodeResult(status=NodeStatus.FAILED, error=str(e))
        # harness SubagentResult: completed=正常结束, capped=预算/轮次违规, failed=异常
        failed = [r for r in raw if r["status"] != "completed"]
        logger.info(f"coordinator_fanout 完成: total={len(raw)} failed={len(failed)}")
        data = {"results": raw, "total": len(raw), "failed": len(failed)}
        if failed and not bool(params.get("allow_partial", False)):
            names = ",".join(r["name"] for r in failed)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"{len(failed)}/{len(raw)} worker 失败: {names}",
                data=data,
            )
        return NodeResult(status=NodeStatus.SUCCESS, data=data, summary=f"{len(raw)} worker 完成 ({len(failed)} 失败)")


@register_node
class SessionForkNode(BaseNode):
    """会话分叉节点 — 波边界 fork 出并行子分支 (各自 session id), 本节点汇聚。"""

    name = "session_fork"
    display_name = "会话分叉"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "Fork the parent session into parallel child branches (own session ids), gather at this merge node"
    icon = "🍴"
    default_label = "会话分叉"

    inputs = [
        {"key": "branches", "label": "分支规格列表", "type": "array"},
    ]
    outputs = [
        {"key": "parent_session", "label": "父 session id", "type": "string"},
        {"key": "branches", "label": "分支结果映射", "type": "object"},
        {"key": "gathered", "label": "是否已汇聚", "type": "boolean"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "branches": {
                    "type": "array",
                    "description": "分支规格 [{name, node, params?}] — node 为已注册节点名",
                },
                "allow_partial": {"type": "boolean", "description": "部分分支失败不整体 FAILED", "default": False},
                "timeout": {"type": "number", "description": "分叉超时秒数 (0 = 不限)", "default": _DEFAULT_TIMEOUT},
            },
            "required": ["branches"],
        }

    async def execute(self, inputs: dict) -> NodeResult:
        params = getattr(self.config, "params", {}) or {}
        branches = params.get("branches") or inputs.get("branches")
        if not isinstance(branches, list) or not branches:
            return NodeResult(status=NodeStatus.FAILED, error="session_fork 参数 branches 必填 (非空数组)")
        parent = _parent_session_id()

        async def run_branch(spec_dict: Dict[str, Any], idx: int) -> tuple:
            name = str(spec_dict.get("name") or f"branch-{idx + 1}")
            child_sid = f"{parent}:fork:{name}"
            try:
                node_name = str(spec_dict.get("node") or "")
                if not node_name:
                    raise ValueError(f"分支 '{name}' 缺 node")
                node = NodeRegistry.create(node_name, config=NodeConfig(params=dict(spec_dict.get("params") or {})))
                if node is None:
                    raise ValueError(f"分支 '{name}' 的节点 '{node_name}' 未注册")
                res = await node.execute({"session_id": child_sid, "parent_session": parent, "branch": name})
                return name, child_sid, res
            except Exception as e:
                logger.warning(f"session_fork branch '{name}' failed: {e}")
                return name, child_sid, NodeResult(status=NodeStatus.FAILED, error=str(e))

        try:
            timeout = float(params.get("timeout", _DEFAULT_TIMEOUT) or 0)
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        logger.info(f"session_fork 开始: parent={parent} branches={len(branches)}")
        try:
            if timeout > 0:
                raw = await asyncio.wait_for(
                    asyncio.gather(*(run_branch(b, i) for i, b in enumerate(branches))), timeout=timeout
                )
            else:
                raw = list(await asyncio.gather(*(run_branch(b, i) for i, b in enumerate(branches))))
        except TimeoutError:
            return NodeResult(status=NodeStatus.FAILED, error=f"session_fork 分叉超时 ({timeout:g}s)")
        branch_out: Dict[str, Any] = {}
        failed_names: List[str] = []
        for name, child_sid, res in raw:
            status = _status_str(res.status)
            branch_out[name] = {
                "session_id": child_sid,
                "status": status,
                "output": dict(res.data) if isinstance(res.data, dict) else {},
                "error": res.error or "",
            }
            if status != "success":
                failed_names.append(name)
        logger.info(f"session_fork 汇聚完成: parent={parent} branches={len(branch_out)} failed={len(failed_names)}")
        data = {"parent_session": parent, "branches": branch_out, "gathered": True}
        if failed_names and not bool(params.get("allow_partial", False)):
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"{len(failed_names)}/{len(branch_out)} 分支失败: {','.join(failed_names)}",
                data=data,
            )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data=data,
            summary=f"{len(branch_out)} 分支汇聚完成 ({len(failed_names)} 失败)",
        )
