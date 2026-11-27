"""Fusion ecosystem interop node — bridges to the fusion-agent-studio graph/agent runtime.

Submits graph/agent runs via agent-studio HTTP REST (default 127.0.0.1:11442):
sync mode waits for the terminal state and collects results (graph sync execute
returns the full event stream), async mode returns a run_id for upstream
orchestration. Auth is optional — trusted local passthrough when the upstream
has no key configured; when FUSION_AGENT_STUDIO_API_KEY is set it rides along
as x-api-key.

References: fusion-agent-studio/agent_runtime/api_server.py (/v1/runs,
/v1/graphs/{id}/execute, /v1/runs/{id}, /v1/runs/{id}/result,
/v1/runs/{id}/cancel)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict

import httpx

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)

logger = logging.getLogger(__name__)

# product-level call (roadmap §6): studio_run submits graph/agent runs to the
# agent-studio daemon — the executed entities live there, not in-process.
_DEFAULT_BASE_URL = "http://127.0.0.1:11442"

# Aligned with agent-studio task_store._VALID_STATUSES terminal states (canceled, single l)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})

_POLL_INTERVAL = 2.0
_POLL_INTERVAL_MIN = 0.05
_POLL_INTERVAL_MAX = 30.0


def _get_base_url() -> str:
    return os.environ.get("FUSION_AGENT_STUDIO_URL", _DEFAULT_BASE_URL).rstrip("/")


def _build_headers() -> Dict[str, str]:
    # Upstream auth optional: trusted local passthrough when no key configured, x-api-key header when set
    key = os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["x-api-key"] = key
    return headers


async def _submit_run(client: httpx.AsyncClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    resp = await client.post("/v1/runs", json=payload)
    resp.raise_for_status()
    return resp.json()


async def _execute_graph(client: httpx.AsyncClient, graph_id: str, input_text: str) -> Dict[str, Any]:
    # issue #135 F-1: upstream GraphExecuteRequest requires graph_id in the body (redundant
    # with the path param but enforced), 422 otherwise
    resp = await client.post(
        f"/v1/graphs/{graph_id}/execute",
        json={"graph_id": graph_id, "input_text": input_text},
    )
    resp.raise_for_status()
    return resp.json()


async def _get_run(client: httpx.AsyncClient, run_id: str) -> Dict[str, Any]:
    resp = await client.get(f"/v1/runs/{run_id}")
    resp.raise_for_status()
    return resp.json()


async def _get_run_result(client: httpx.AsyncClient, run_id: str) -> Dict[str, Any]:
    resp = await client.get(f"/v1/runs/{run_id}/result")
    resp.raise_for_status()
    return resp.json()


def _clamp_poll_interval(value: float) -> float:
    if value < _POLL_INTERVAL_MIN:
        return _POLL_INTERVAL_MIN
    if value > _POLL_INTERVAL_MAX:
        return _POLL_INTERVAL_MAX
    return value


# issue #373: budget exhaustion marker from agent-studio terminal failures —
# e.g. "Max iterations exceeded after 24/24 turns (249019 tokens, report_len=0)"
_BUDGET_EXHAUSTED_MARKER = "max iterations"


def _is_budget_exhausted(res: NodeResult) -> bool:
    result = res.data.get("result") if isinstance(res.data, dict) else None
    last_error = str(result.get("last_error", "") or "") if isinstance(result, dict) else ""
    return _BUDGET_EXHAUSTED_MARKER in last_error.lower()


@register_node
class StudioRunNode(BaseNode):
    """Studio run node — invokes fusion-agent-studio to run a graph/agent from within a cowork DAG.

    graph + sync: POST /v1/graphs/{id}/execute returns the full event stream directly;
    otherwise: POST /v1/runs submits, sync polls until terminal then fetches /result,
    async returns run_id.
    """

    name = "studio_run"
    display_name = "Studio 运行"  # i18n: user-facing
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "调用 fusion-agent-studio 运行 graph/agent (sync 等待终态 / async 返回 run_id)"  # i18n: user-facing
    icon = "🎬"
    default_label = "Studio 运行"  # i18n: user-facing

    inputs = [
        # i18n: user-facing
        {"key": "input", "label": "输入文本", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "run_id", "label": "运行 ID", "type": "string"},
        # i18n: user-facing
        {"key": "status", "label": "运行状态", "type": "string"},
        # i18n: user-facing
        {"key": "result", "label": "运行结果", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "graph_id": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "agent-studio graph ID (与 agent_id 二选一)",
                },
                "agent_id": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "agent-studio agent ID (与 graph_id 二选一)",
                },
                "input": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "输入文本 (graph execute 的 input_text / run 的 goal)",
                },
                "mode": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "sync 等待终态 / async 提交即返回 run_id",
                    "enum": ["sync", "async"],
                    "default": "sync",
                },
                "timeout": {
                    "type": "number",
                    # i18n: user-facing
                    "description": "sync 模式等待终态的总超时秒数 (缺省 1800, 可用 FUSION_COWORK_STUDIO_RUN_TIMEOUT 覆盖)",
                    "default": 1800.0,
                },
                "poll_interval": {
                    "type": "number",
                    # i18n: user-facing
                    "description": "sync 模式轮询间隔秒数",
                    "default": 2.0,
                },
                "title": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "run 标题 (缺省取 input 前 80 字符, 上游默认行为)",
                },
                "idempotency_key": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "幂等键 (重复提交去重, 上游 deduped 标记)",
                },
                "max_turns": {
                    "type": "number",
                    # i18n: user-facing
                    "description": (
                        "run 轮次预算 (经 /v1/runs body spec.max_turns 传入; 缺省不传 — 上游按 agent "  # i18n: user-facing
                        "manifest 或 studio 默认 8 轮; 多产物角色建议 24-48, issue #363/#373)"  # i18n: user-facing
                    ),
                },
                # issue #387: seat model pinning — passed via /v1/runs body spec.model; omitted by default
                # (upstream picks per agent manifest); replay-dev = zero-cost deterministic replay (fusion-mlx#1518)
                "model": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "座位模型 id (spec.model; 缺省不传; 合成 E2E 用 replay-dev)",
                },
                "report_recovery": {
                    "type": "boolean",
                    # i18n: user-facing
                    "description": (
                        "run 因 Max iterations 耗尽轮次且报告为空时, 自动补一轮 report-only 续跑 "  # i18n: user-facing
                        "(交付物通常已写入 workspace; 缺省开启, issue #373)"  # i18n: user-facing
                    ),
                    "default": True,
                },
            },
            "required": ["input"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        graph_id = str(inputs.get("graph_id", params.get("graph_id", "")) or "")
        agent_id = str(inputs.get("agent_id", params.get("agent_id", "")) or "")
        text = str(inputs.get("input", params.get("input", "")) or "")
        mode = str(params.get("mode", "sync") or "sync")
        # issue #351: the default 300s would cancel healthy long 4B seat generations — 900s minimum;
        # issue #409: under concurrent-pipeline mlx contention even 900s is not enough (upstream only
        # reached terminal state after 460s while the pipeline had already declared death) — default raised
        # to 1800s, globally tunable via the FUSION_COWORK_STUDIO_RUN_TIMEOUT env var
        timeout = params.get("timeout") or float(os.environ.get("FUSION_COWORK_STUDIO_RUN_TIMEOUT") or 1800.0)
        poll_interval = _clamp_poll_interval(float(params.get("poll_interval", _POLL_INTERVAL) or _POLL_INTERVAL))
        title = str(params.get("title", "") or "")
        idempotency_key = str(params.get("idempotency_key", "") or "")
        max_turns = params.get("max_turns")
        model = str(params.get("model", "") or "")
        report_recovery = params.get("report_recovery", True)
        if report_recovery is None:
            report_recovery = True

        if not text:
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error="缺少必填参数 input",
                # i18n: user-facing
                summary="参数不完整",
            )
        if bool(graph_id) == bool(agent_id):
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error="缺少必填参数 graph_id 或 agent_id (二选一, 不可同时为空或同时设置)",
                # i18n: user-facing
                summary="参数不完整",
            )

        headers = _build_headers()
        logger.info(
            "StudioRunNode start mode=%s graph_id=%r agent_id=%r timeout=%s poll=%s",
            mode,
            graph_id,
            agent_id,
            timeout,
            poll_interval,
        )
        try:
            async with httpx.AsyncClient(
                base_url=_get_base_url(),
                headers=headers,
                timeout=timeout if timeout > 0 else 300.0,
            ) as client:
                if graph_id and mode == "sync":
                    return await self._run_graph_sync(client, graph_id, text)
                payload: Dict[str, Any] = {"goal": text}
                if graph_id:
                    payload["graph_id"] = graph_id
                if agent_id:
                    payload["agent_id"] = agent_id
                if title:
                    payload["title"] = title
                if idempotency_key:
                    payload["idempotency_key"] = idempotency_key
                if max_turns or model:
                    # issue #363: explicit turn budget — studio's default 8 turns starves multi-artifact roles
                    # issue #387: spec.model seat model pinning (synthetic E2E pins replay-dev)
                    spec: Dict[str, Any] = {}
                    if max_turns:
                        spec["max_turns"] = int(max_turns)
                    if model:
                        spec["model"] = model
                    payload["spec"] = spec
                submitted = await _submit_run(client, payload)
                run_id = str(submitted.get("run_id", "") or "")
                if not run_id:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"上游未返回 run_id: {submitted}",  # i18n: user-facing
                        summary="提交失败",  # i18n: user-facing
                    )
                if mode == "async":
                    logger.info("StudioRunNode submitted run_id=%s (async)", run_id)
                    return NodeResult(
                        status=NodeStatus.SUCCESS,
                        data={"run_id": run_id, "status": str(submitted.get("status", "pending")), "result": {}},
                        summary=f"已提交 run {run_id}",  # i18n: user-facing
                    )
                res = await self._await_terminal(client, run_id, timeout, poll_interval)
                # issue #373: budget exhausted with an empty report — deliverables are usually already
                # in the workspace; add one report-only continuation run to recover the final report
                # instead of hard-failing
                if res.status is NodeStatus.FAILED and report_recovery and _is_budget_exhausted(res):
                    recovered = await self._report_recovery(client, payload, run_id, timeout, poll_interval)
                    if recovered is not None:
                        return recovered
                return res
        except httpx.HTTPStatusError as e:
            body = ""
            if e.response is not None:
                body = e.response.text[:200]
            logger.error("StudioRunNode HTTP %s: %s", e.response.status_code if e.response is not None else "?", body)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"agent-studio HTTP {e.response.status_code if e.response is not None else '?'}: {body}",
                summary="上游 HTTP 错误",  # i18n: user-facing
            )
        except Exception as e:
            logger.exception("StudioRunNode execution exception")
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=f"Studio 运行异常: {e}",
                # i18n: user-facing
                summary="执行异常",
            )

    async def _run_graph_sync(self, client: httpx.AsyncClient, graph_id: str, text: str) -> NodeResult:
        payload = await _execute_graph(client, graph_id, text)
        status = str(payload.get("status", ""))
        events = payload.get("events", [])
        if status != "completed":
            logger.warning("StudioRunNode graph %s not completed: %s", graph_id, status)
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=f"graph {graph_id} 执行状态 {status or 'unknown'}",
                data={"run_id": "", "status": status or "unknown", "result": payload},
                # i18n: user-facing
                summary="graph 执行未完成",
            )
        logger.info("StudioRunNode graph %s completed: %d events", graph_id, len(events))
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"run_id": "", "status": status, "result": payload},
            # i18n: user-facing
            summary=f"graph 执行完成 ({len(events)} events)",
        )

    async def _report_recovery(
        self,
        client: httpx.AsyncClient,
        payload: Dict[str, Any],
        original_run_id: str,
        timeout: float,
        poll_interval: float,
    ) -> NodeResult | None:
        # issue #373: one report-only continuation run on budget exhaustion.
        # Returns None on any recovery failure — original failure surfaces unchanged.
        recovery_payload = dict(payload)
        recovery_payload["goal"] = (
            "REPORT ONLY - the previous run exhausted its turn budget. All work products were "
            "already written to the workspace. Do NOT redo the work. Inspect the workspace and "
            f"write the final report for run {original_run_id}."
        )
        # idempotency key belongs to the original run — reuse would dedup into it
        recovery_payload.pop("idempotency_key", None)
        logger.warning("StudioRunNode run %s budget exhausted, submitting report-only recovery run", original_run_id)
        try:
            submitted = await _submit_run(client, recovery_payload)
            new_run_id = str(submitted.get("run_id", "") or "")
            if not new_run_id:
                logger.warning("StudioRunNode report recovery submit returned no run_id: %s", submitted)
                return None
            res = await self._await_terminal(client, new_run_id, timeout, poll_interval)
        except Exception as e:
            logger.warning("StudioRunNode report recovery after %s failed: %s", original_run_id, e)
            return None
        if res.status is not NodeStatus.SUCCESS:
            logger.warning("StudioRunNode report recovery run %s did not complete: %s", new_run_id, res.error)
            return None
        logger.info("StudioRunNode report recovery run %s completed (from %s)", new_run_id, original_run_id)
        res.summary = f"budget exhausted on {original_run_id}, report-only recovery run {new_run_id} completed"
        res.data["report_recovery"] = True
        res.data["recovered_from"] = original_run_id
        return res

    async def _await_terminal(
        self,
        client: httpx.AsyncClient,
        run_id: str,
        timeout: float,
        poll_interval: float,
    ) -> NodeResult:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            run = await _get_run(client, run_id)
            status = str(run.get("status", ""))
            if status in _TERMINAL_STATUSES:
                break
            if time.monotonic() >= deadline:
                # issue #351: no cancel on timeout — canceling would discard artifacts a healthy run
                # has already produced (its generation duration was merely jittery); keep the run
                # executing upstream, results queryable/reclaimable via run_id
                logger.warning(
                    "StudioRunNode run %s terminal-state wait timed out (%.0fs), not canceling (run kept executing upstream)",
                    run_id,
                    timeout,
                )
                return NodeResult(
                    status=NodeStatus.FAILED,
                    # i18n: user-facing
                    error=(
                        f"等待 run {run_id} 终态超时 ({timeout:.0f}s), run 已保留在上游继续执行 (未取消), "  # i18n: user-facing
                        "可凭 run_id 在 agent-studio 查询/回收结果"  # i18n: user-facing
                    ),
                    data={"run_id": run_id, "status": status or "unknown", "result": {}, "detached": True},
                    summary="等待终态超时 (run 保留, 未取消)",  # i18n: user-facing
                )
            await asyncio.sleep(poll_interval)
        logger.info("StudioRunNode run %s terminal: %s", run_id, status)
        result = await _get_run_result(client, run_id)
        artifacts = result.get("artifacts", []) if isinstance(result, dict) else []
        if status != "completed":
            last_error = str(result.get("last_error", "") or "") if isinstance(result, dict) else ""
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=f"run {run_id} 终态 {status}: {last_error or '无错误详情'}",
                data={"run_id": run_id, "status": status, "result": result},
                summary=f"run 终态 {status}",  # i18n: user-facing
            )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"run_id": run_id, "status": status, "result": result},
            summary=f"run {run_id} 完成 ({len(artifacts)} artifacts)",  # i18n: user-facing
        )
