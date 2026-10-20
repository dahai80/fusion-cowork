"""Fusion 生态互通节点 — 对接 fusion-agent-studio 的 graph/agent 运行时。

通过 agent-studio HTTP REST (默认 127.0.0.1:11442) 提交 graph/agent 运行:
sync 模式等待终态收集结果 (graph 同步 execute 拿全量 events), async 模式
返回 run_id 由上游编排。鉴权可选 — 上游未配 key 时本地受信放行, 配置了
FUSION_AGENT_STUDIO_API_KEY 则随请求携带 (x-api-key)。

参考: fusion-agent-studio/agent_runtime/api_server.py (/v1/runs,
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

_DEFAULT_BASE_URL = "http://127.0.0.1:11442"

# 对齐 agent-studio task_store._VALID_STATUSES 终态 (canceled 单 l)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})

_POLL_INTERVAL = 2.0
_POLL_INTERVAL_MIN = 0.05
_POLL_INTERVAL_MAX = 30.0


def _get_base_url() -> str:
    return os.environ.get("FUSION_AGENT_STUDIO_URL", _DEFAULT_BASE_URL).rstrip("/")


def _build_headers() -> Dict[str, str]:
    # 上游鉴权可选: 未配置 key 时本地受信放行, 有则 x-api-key 携带
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
    resp = await client.post(
        f"/v1/graphs/{graph_id}/execute",
        json={"input_text": input_text},
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


async def _cancel_run(client: httpx.AsyncClient, run_id: str) -> None:
    resp = await client.post(f"/v1/runs/{run_id}/cancel")
    resp.raise_for_status()


def _clamp_poll_interval(value: float) -> float:
    if value < _POLL_INTERVAL_MIN:
        return _POLL_INTERVAL_MIN
    if value > _POLL_INTERVAL_MAX:
        return _POLL_INTERVAL_MAX
    return value


@register_node
class StudioRunNode(BaseNode):
    """Studio 运行节点 — cowork DAG 内调用 fusion-agent-studio 运行 graph/agent。

    graph + sync: POST /v1/graphs/{id}/execute 直接拿全量 events;
    其余: POST /v1/runs 提交, sync 轮询至终态后取 /result, async 返回 run_id。
    """

    name = "studio_run"
    display_name = "Studio 运行"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "调用 fusion-agent-studio 运行 graph/agent (sync 等待终态 / async 返回 run_id)"
    icon = "🎬"
    default_label = "Studio 运行"

    inputs = [
        {"key": "input", "label": "输入文本", "type": "string"},
    ]
    outputs = [
        {"key": "run_id", "label": "运行 ID", "type": "string"},
        {"key": "status", "label": "运行状态", "type": "string"},
        {"key": "result", "label": "运行结果", "type": "object"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "graph_id": {
                    "type": "string",
                    "description": "agent-studio graph ID (与 agent_id 二选一)",
                },
                "agent_id": {
                    "type": "string",
                    "description": "agent-studio agent ID (与 graph_id 二选一)",
                },
                "input": {
                    "type": "string",
                    "description": "输入文本 (graph execute 的 input_text / run 的 goal)",
                },
                "mode": {
                    "type": "string",
                    "description": "sync 等待终态 / async 提交即返回 run_id",
                    "enum": ["sync", "async"],
                    "default": "sync",
                },
                "timeout": {
                    "type": "number",
                    "description": "sync 模式等待终态的总超时秒数",
                    "default": 300.0,
                },
                "poll_interval": {
                    "type": "number",
                    "description": "sync 模式轮询间隔秒数",
                    "default": 2.0,
                },
                "title": {
                    "type": "string",
                    "description": "run 标题 (缺省取 input 前 80 字符, 上游默认行为)",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "幂等键 (重复提交去重, 上游 deduped 标记)",
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
        timeout = params.get("timeout", 300.0) or 300.0
        poll_interval = _clamp_poll_interval(float(params.get("poll_interval", _POLL_INTERVAL) or _POLL_INTERVAL))
        title = str(params.get("title", "") or "")
        idempotency_key = str(params.get("idempotency_key", "") or "")

        if not text:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="缺少必填参数 input",
                summary="参数不完整",
            )
        if bool(graph_id) == bool(agent_id):
            return NodeResult(
                status=NodeStatus.FAILED,
                error="缺少必填参数 graph_id 或 agent_id (二选一, 不可同时为空或同时设置)",
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
                submitted = await _submit_run(client, payload)
                run_id = str(submitted.get("run_id", "") or "")
                if not run_id:
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"上游未返回 run_id: {submitted}",
                        summary="提交失败",
                    )
                if mode == "async":
                    logger.info("StudioRunNode submitted run_id=%s (async)", run_id)
                    return NodeResult(
                        status=NodeStatus.SUCCESS,
                        data={"run_id": run_id, "status": str(submitted.get("status", "pending")), "result": {}},
                        summary=f"已提交 run {run_id}",
                    )
                return await self._await_terminal(client, run_id, timeout, poll_interval)
        except httpx.HTTPStatusError as e:
            body = ""
            if e.response is not None:
                body = e.response.text[:200]
            logger.error("StudioRunNode HTTP %s: %s", e.response.status_code if e.response is not None else "?", body)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"agent-studio HTTP {e.response.status_code if e.response is not None else '?'}: {body}",
                summary="上游 HTTP 错误",
            )
        except Exception as e:
            logger.exception("StudioRunNode 执行异常")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"Studio 运行异常: {e}",
                summary="执行异常",
            )

    async def _run_graph_sync(self, client: httpx.AsyncClient, graph_id: str, text: str) -> NodeResult:
        payload = await _execute_graph(client, graph_id, text)
        status = str(payload.get("status", ""))
        events = payload.get("events", [])
        if status != "completed":
            logger.warning("StudioRunNode graph %s 非完成态: %s", graph_id, status)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"graph {graph_id} 执行状态 {status or 'unknown'}",
                data={"run_id": "", "status": status or "unknown", "result": payload},
                summary="graph 执行未完成",
            )
        logger.info("StudioRunNode graph %s 完成: %d events", graph_id, len(events))
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"run_id": "", "status": status, "result": payload},
            summary=f"graph 执行完成 ({len(events)} events)",
        )

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
                logger.warning("StudioRunNode run %s 等待终态超时 (%.0fs), 尽力取消", run_id, timeout)
                cancel_error = ""
                try:
                    await _cancel_run(client, run_id)
                except Exception as exc:
                    logger.warning("StudioRunNode run %s 取消失败: %s", run_id, exc)
                    cancel_error = f" (取消失败: {exc})"
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"等待 run {run_id} 终态超时 ({timeout:.0f}s), 已尽力取消{cancel_error}",
                    data={"run_id": run_id, "status": status or "unknown", "result": {}},
                    summary="等待终态超时",
                )
            await asyncio.sleep(poll_interval)
        logger.info("StudioRunNode run %s 终态: %s", run_id, status)
        result = await _get_run_result(client, run_id)
        artifacts = result.get("artifacts", []) if isinstance(result, dict) else []
        if status != "completed":
            last_error = str(result.get("last_error", "") or "") if isinstance(result, dict) else ""
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"run {run_id} 终态 {status}: {last_error or '无错误详情'}",
                data={"run_id": run_id, "status": status, "result": result},
                summary=f"run 终态 {status}",
            )
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"run_id": run_id, "status": status, "result": result},
            summary=f"run {run_id} 完成 ({len(artifacts)} artifacts)",
        )
