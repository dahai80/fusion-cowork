import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Protocol

import httpx

from .node import BaseNode, NodeResult, NodeStatus

logger = logging.getLogger(__name__)

EXECUTOR_SIMPLE = "simple"
EXECUTOR_AGENT = "agent"
KNOWN_EXECUTORS = (EXECUTOR_SIMPLE, EXECUTOR_AGENT)

_AS_DEFAULT_SOCKET = "/tmp/fusion-studio.sock"
# product-level call (roadmap §6): AgentExecutor submits/polls agent runs on
# the agent-studio daemon (UDS or HTTP) — the agent runtime lives there.
_AS_DEFAULT_URL = "http://127.0.0.1:11442"
_AS_TERMINAL = frozenset({"completed", "failed", "canceled"})
_AS_POLL_INTERVAL = 2.0
_AS_HTTP_TIMEOUT = 600.0
_AGENT_DEFAULT_MAX_TURNS = 8


@dataclass
class NodeExecutionContext:
    execution_id: str = ""
    workflow_id: str = ""
    workflow_name: str = ""
    node_id: str = ""
    attempt: int = 1


class NodeExecutor(Protocol):
    name: str

    async def execute(self, node: BaseNode, inputs: Dict[str, Any], ctx: NodeExecutionContext) -> NodeResult: ...


class SimpleExecutor:
    name = EXECUTOR_SIMPLE

    async def execute(self, node: BaseNode, inputs: Dict[str, Any], ctx: NodeExecutionContext) -> NodeResult:
        return await node.execute(inputs)


def _as_socket_path() -> str:
    return os.environ.get("FUSION_AGENT_STUDIO_SOCKET", "").strip() or _AS_DEFAULT_SOCKET


def _as_base_url() -> str:
    return os.environ.get("FUSION_AGENT_STUDIO_URL", _AS_DEFAULT_URL).strip().rstrip("/")


def _as_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "").strip()
    if key:
        headers["x-api-key"] = key
    return headers


def _as_transport() -> str:
    forced = os.environ.get("FUSION_COWORK_AGENT_TRANSPORT", "").strip().lower()
    if forced in ("uds", "http"):
        return forced
    return "uds" if os.path.exists(_as_socket_path()) else "http"


async def _uds_call(method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    socket_path = _as_socket_path()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
        writer.write(payload.encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    if not raw:
        raise RuntimeError(f"agent-studio returned empty response: {method}")
    frame = json.loads(raw.decode())
    if isinstance(frame, dict) and frame.get("error"):
        err = frame["error"]
        message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise RuntimeError(f"agent-studio RPC error {method}: {message}")
    result = frame.get("result", {}) if isinstance(frame, dict) else {}
    return result if isinstance(result, dict) else {}


def _agent_task_text(inputs: Dict[str, Any], params: Dict[str, Any]) -> str:
    for key in ("input", "task", "prompt", "goal", "text"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("task", "prompt", "input"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if inputs:
        try:
            return json.dumps(inputs, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(inputs)
    return ""


def _count_tool_calls(transcript: Dict[str, Any]) -> int:
    messages = transcript.get("messages") if isinstance(transcript, dict) else None
    if not isinstance(messages, list):
        return 0
    count = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if isinstance(block, dict) and str(block.get("type", "")) in ("tool_use", "tool_call"):
                count += 1
    return count


async def _agent_via_uds(node: BaseNode, task: str, params: Dict[str, Any], timeout: float) -> NodeResult:
    cfg = node.config
    spec: Dict[str, Any] = {
        "name": str(params.get("agent") or cfg.agent or f"cowork-{node.id[:8]}"),
        "task": task,
        "model": str(cfg.model or params.get("model") or ""),
        "tools": list(cfg.tools or []),
        "max_turns": int(cfg.max_turns or _AGENT_DEFAULT_MAX_TURNS),
        "budget_tokens": int(cfg.budget_tokens or 0),
    }
    inner_timeout = float(timeout or 0) or _AS_HTTP_TIMEOUT
    logger.info(
        "AgentExecutor uds run node=%s spec_name=%s tools=%d model=%r max_turns=%s budget_tokens=%s",
        node.name,
        spec["name"],
        len(spec["tools"]),
        spec["model"],
        spec["max_turns"],
        spec["budget_tokens"],
    )
    result = await _uds_call("subagent.run", {"spec": spec}, inner_timeout)
    report = result.get("report") if isinstance(result, dict) else None
    if not isinstance(report, dict):
        return NodeResult(
            status=NodeStatus.FAILED,
            error=f"agent-studio returned no report: {str(result)[:200]}",
            summary="agent run failed",
            data={"executor": EXECUTOR_AGENT, "transport": "uds"},
        )
    turns = int(report.get("turns", 0) or 0)
    tokens = int(report.get("tokens_used", 0) or 0)
    agent_id = str(report.get("agent_id", "") or "")
    tool_calls = 0
    if agent_id:
        try:
            transcript = await _uds_call("subagent.transcript", {"agent_id": agent_id}, 30.0)
            tool_calls = _count_tool_calls(transcript.get("transcript", {}) if isinstance(transcript, dict) else {})
        except Exception as exc:
            logger.warning("AgentExecutor failed to read transcript (tool_calls counted as 0): %s", exc)
    data = {
        "executor": EXECUTOR_AGENT,
        "transport": "uds",
        "agent_id": agent_id,
        "turns": turns,
        "tool_calls": tool_calls,
        "result": str(report.get("report", "") or ""),
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": tokens},
    }
    logger.info(
        "AgentExecutor uds done node=%s ok=%s turns=%s tool_calls=%s tokens=%s partial=%s",
        node.name,
        report.get("ok"),
        turns,
        tool_calls,
        tokens,
        report.get("partial"),
    )
    if not report.get("ok"):
        return NodeResult(
            status=NodeStatus.FAILED,
            error=str(report.get("error", "") or "agent run did not succeed"),
            data=data,
            summary=f"agent run failed (turns={turns})",
        )
    summary = f"agent completed turns={turns} tool_calls={tool_calls} tokens={tokens}"
    if report.get("partial"):
        summary += " (max_turns limit not reached, marked partial)"
    return NodeResult(status=NodeStatus.SUCCESS, data=data, summary=summary)


async def _agent_via_http(node: BaseNode, task: str, params: Dict[str, Any], timeout: float) -> NodeResult:
    cfg = node.config
    agent_id = str(cfg.agent or params.get("agent_id") or "")
    graph_id = str(params.get("graph_id") or "")
    if not agent_id and not graph_id:
        return NodeResult(
            status=NodeStatus.FAILED,
            error="agent executor missing agent (agent-studio agent id) or graph_id",
            summary="incomplete parameters",
            data={"executor": EXECUTOR_AGENT, "transport": "http"},
        )
    ignored = [
        key
        for key, value in (
            ("tools", cfg.tools),
            ("model", cfg.model),
            ("max_turns", cfg.max_turns),
            ("budget_tokens", cfg.budget_tokens),
        )
        if value
    ]
    if ignored:
        logger.warning(
            "AgentExecutor http channel does not support per-node overrides %s; this run follows the agent definition in agent-studio",
            ignored,
        )
    payload: Dict[str, Any] = {"goal": task, "title": str(params.get("title") or f"cowork {node.name}")}
    if agent_id:
        payload["agent_id"] = agent_id
    if graph_id:
        payload["graph_id"] = graph_id
    wait = float(timeout or 0) or _AS_HTTP_TIMEOUT
    deadline = time.monotonic() + wait
    logger.info("AgentExecutor http run node=%s agent_id=%r graph_id=%r", node.name, agent_id, graph_id)
    async with httpx.AsyncClient(base_url=_as_base_url(), headers=_as_headers(), timeout=wait) as client:
        resp = await client.post("/v1/runs", json=payload)
        resp.raise_for_status()
        submitted = resp.json()
        run_id = str(submitted.get("run_id", "") or "")
        if not run_id:
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"agent-studio returned no run_id: {submitted}",
                summary="submission failed",
                data={"executor": EXECUTOR_AGENT, "transport": "http"},
            )
        status = ""
        while True:
            run = (await client.get(f"/v1/runs/{run_id}")).json()
            status = str(run.get("status", "") or "")
            if status in _AS_TERMINAL:
                break
            if time.monotonic() >= deadline:
                try:
                    await client.post(f"/v1/runs/{run_id}/cancel")
                except Exception as exc:
                    logger.warning("AgentExecutor failed to cancel run %s: %s", run_id, exc)
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"timed out waiting for run {run_id} terminal state ({wait:.0f}s), best-effort cancel issued",
                    data={"executor": EXECUTOR_AGENT, "transport": "http", "run_id": run_id, "status": status},
                    summary="timed out waiting for terminal state",
                )
            await asyncio.sleep(_AS_POLL_INTERVAL)
        result = (await client.get(f"/v1/runs/{run_id}/result")).json()
    events = int(((result.get("steps") or [{}])[0] or {}).get("events", 0) or 0) if isinstance(result, dict) else 0
    data = {
        "executor": EXECUTOR_AGENT,
        "transport": "http",
        "run_id": run_id,
        "status": status,
        "turns": 0,
        "tool_calls": 0,
        "events": events,
        "result": result.get("last_result", {}) if isinstance(result, dict) else {},
    }
    logger.info("AgentExecutor http done node=%s run_id=%s status=%s events=%s", node.name, run_id, status, events)
    if status != "completed":
        error = (
            str(result.get("last_error", "") or f"run {run_id} ended with terminal status {status}") if isinstance(result, dict) else status
        )
        return NodeResult(status=NodeStatus.FAILED, error=error, data=data, summary=f"agent run {status}")
    return NodeResult(
        status=NodeStatus.SUCCESS,
        data=data,
        summary=f"agent completed run={run_id} events={events} (http channel does not report turns)",
    )


class AgentExecutor:
    name = EXECUTOR_AGENT

    async def execute(self, node: BaseNode, inputs: Dict[str, Any], ctx: NodeExecutionContext) -> NodeResult:
        params = node.config.params or {}
        task = _agent_task_text(inputs, params)
        if not task:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="agent executor missing task text (input/task/prompt)",
                summary="incomplete parameters",
                data={"executor": EXECUTOR_AGENT},
            )
        timeout = float(node.config.timeout or 0)
        transport = _as_transport()
        try:
            if transport == "uds":
                return await _agent_via_uds(node, task, params, timeout)
            return await _agent_via_http(node, task, params, timeout)
        except FileNotFoundError as exc:
            logger.error("AgentExecutor could not find agent-studio daemon socket %s: %s", _as_socket_path(), exc)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"agent-studio daemon socket unavailable ({_as_socket_path()}): {exc}",
                summary="agent runtime unavailable",
                data={"executor": EXECUTOR_AGENT, "transport": transport},
            )
        except Exception as exc:
            logger.exception("AgentExecutor execution error node=%s", node.name)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"agent execution error: {exc}",
                summary="execution error",
                data={"executor": EXECUTOR_AGENT, "transport": transport},
            )


_EXECUTORS: Dict[str, NodeExecutor] = {
    EXECUTOR_SIMPLE: SimpleExecutor(),
    EXECUTOR_AGENT: AgentExecutor(),
}


def get_executor(name: str) -> NodeExecutor:
    key = str(name or EXECUTOR_SIMPLE).strip().lower()
    executor = _EXECUTORS.get(key)
    if executor is None:
        logger.warning(f"unknown node executor {name!r}, falling back to {EXECUTOR_SIMPLE}")
        return _EXECUTORS[EXECUTOR_SIMPLE]
    return executor


def executor_for_node(node: BaseNode) -> NodeExecutor:
    return get_executor(getattr(node.config, "executor", EXECUTOR_SIMPLE))


def executor_names(nodes: List[BaseNode]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for node in nodes:
        name = executor_for_node(node).name
        counts[name] = counts.get(name, 0) + 1
    return counts
