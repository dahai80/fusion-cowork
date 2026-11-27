"""Fusion ecosystem interop node — bridges to the fusion-agent-studio graph/agent runtime.

Submits graph/agent runs via agent-studio HTTP REST (default 127.0.0.1:11442):
sync mode waits for terminal state and collects results (graph sync execute
returns full events), async mode returns run_id for upstream orchestration.
Auth is optional — local trusted access when the upstream has no key configured;
when FUSION_AGENT_STUDIO_API_KEY is set it rides every request (x-api-key).

Reference: fusion-agent-studio/agent_runtime/api_server.py (/v1/runs,
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

# aligned with agent-studio task_store._VALID_STATUSES terminal states (single-l "canceled")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})

_POLL_INTERVAL = 2.0
_POLL_INTERVAL_MIN = 0.05
_POLL_INTERVAL_MAX = 30.0


def _get_base_url() -> str:
    return os.environ.get("FUSION_AGENT_STUDIO_URL", _DEFAULT_BASE_URL).rstrip("/")


def _build_headers() -> Dict[str, str]:
    # upstream auth optional: locally trusted when no key configured, x-api-key header when set
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
    # with the path param but mandatory), missing -> 422
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
    """Studio run node — invoke fusion-agent-studio graph/agent runs from within a cowork DAG.

    graph + sync: POST /v1/graphs/{id}/execute returns full events directly;
    otherwise: submit via POST /v1/runs, sync polls to terminal state then fetches
    /result, async returns run_id.
    """

    name = "studio_run"
    # i18n: user-facing
    display_name = "Studio 运行"
    category = NodeCategory.FUSION_ECOSYSTEM
    # i18n: user-facing
    description = "调用 fusion-agent-studio 运行 graph/agent (sync 等待终态 / async 返回 run_id)"
    icon = "🎬"
    # i18n: user-facing
    default_label = "Studio 运行"

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
                        # i18n: user-facing
                        "run 轮次预算 (经 /v1/runs body spec.max_turns 传入; 缺省不传 — 上游按 agent "
                        # i18n: user-facing
                        "manifest 或 studio 默认 8 轮; 多产物角色建议 24-48, issue #363/#373)"
                    ),
                },
                # issue #387: seat model pinning — passed via /v1/runs body spec.model; omitted by
                # default (upstream picks by agent manifest); replay-dev = zero-cost deterministic
                # replay (fusion-mlx#1518)
                "model": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "座位模型 id (spec.model; 缺省不传; 合成 E2E 用 replay-dev)",
                },
                "report_recovery": {
                    "type": "boolean",
                    # i18n: user-facing
                    "description": (
                        # i18n: user-facing
                        "run 因 Max iterations 耗尽轮次且报告为空时, 自动补一轮 report-only 续跑 "
                        # i18n: user-facing
                        "(交付物通常已写入 workspace; 缺省开启, issue #373)"
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
        # issue #351: the legacy 300s default cancels healthy long 4B seat generations — 900s floor;
        # issue #409: under concurrent-pipeline mlx contention even 900s is not enough (upstream
        # terminates after 460s while the pipeline already declares death) — default raised to 1800s,
        # globally adjustable via the FUSION_COWORK_STUDIO_RUN_TIMEOUT env var
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
                error="missing required param input",
                summary="incomplete params",
            )
        if bool(graph_id) == bool(agent_id):
            return NodeResult(
                status=NodeStatus.FAILED,
                error="missing required param graph_id or agent_id (exactly one; both empty or both set is invalid)",
                summary="incomplete params",
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
                    # issue #363: explicit turn budget — the studio default 8 turns starves multi-artifact roles
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
                        error=f"upstream returned no run_id: {submitted}",
                        summary="submit failed",
                    )
                if mode == "async":
                    logger.info("StudioRunNode submitted run_id=%s (async)", run_id)
                    return NodeResult(
                        status=NodeStatus.SUCCESS,
                        data={"run_id": run_id, "status": str(submitted.get("status", "pending")), "result": {}},
                        summary=f"submitted run {run_id}",
                    )
                res = await self._await_terminal(client, run_id, timeout, poll_interval)
                # issue #373: budget exhausted with empty report — deliverables are usually already in
                # the workspace, add one report-only continuation run to recover the final report
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
                summary="upstream HTTP error",
            )
        except Exception as e:
            logger.exception("StudioRunNode execution exception")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"studio run exception: {e}",
                summary="execution exception",
            )

    async def _run_graph_sync(self, client: httpx.AsyncClient, graph_id: str, text: str) -> NodeResult:
        payload = await _execute_graph(client, graph_id, text)
        status = str(payload.get("status", ""))
        events = payload.get("events", [])
        if status != "completed":
            logger.warning("StudioRunNode graph %s non-completed status: %s", graph_id, status)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"graph {graph_id} execution status {status or 'unknown'}",
                data={"run_id": "", "status": status or "unknown", "result": payload},
                summary="graph execution not completed",
            )
        logger.info("StudioRunNode graph %s finished: %d events", graph_id, len(events))
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"run_id": "", "status": status, "result": payload},
            summary=f"graph execution finished ({len(events)} events)",
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
                # issue #351: do NOT cancel on timeout — cancellation would discard artifacts a healthy
                # run already completed (mere generation-time jitter); keep the run executing upstream,
                # results can be queried/recovered later by run_id
                logger.warning(
                    "StudioRunNode run %s terminal-state wait timed out (%.0fs), not cancelling (run keeps executing upstream)",
                    run_id,
                    timeout,
                )
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=(
                        f"timed out waiting for run {run_id} terminal state ({timeout:.0f}s), run kept executing "
                        "upstream (not cancelled); query/recover results in agent-studio by run_id"
                    ),
                    data={"run_id": run_id, "status": status or "unknown", "result": {}, "detached": True},
                    summary="terminal-state wait timed out (run kept, not cancelled)",
                )
            await asyncio.sleep(poll_interval)
        logger.info("StudioRunNode run %s terminal state: %s", run_id, status)
        result = await _get_run_result(client, run_id)
        artifacts = result.get("artifacts", []) if isinstance(result, dict) else []
        if status != "completed":
            last_error = str(result.get("last_error", "") or "") if isinstance(result, dict) else ""
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"run {run_id} terminal status {status}: {last_error or 'no error detail'}",
                data={"run_id": run_id, "status": status, "result": result},
                summary=f"run terminal status {status}",
            )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"run_id": run_id, "status": status, "result": result},
            summary=f"run {run_id} finished ({len(artifacts)} artifacts)",
        )
