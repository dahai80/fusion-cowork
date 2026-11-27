"""Fusion ecosystem interop node — drive team members via agent-studio teams.* RPC (issue #488).

Design boundary (fusion-team-architecture-2026-11-14.md §5.4): DAG orchestration
stays in cowork; the node handler is just "one team call" — invoking agent-studio's
teams.add_task / teams.run_member / teams.status (JSON-RPC 2.0, POST /v1/rpc,
same-tier auth as subagent.*), never moving team logic into the cowork engine.
When T3 (agent-studio teams.* dispatcher) is not deployed the node fails loudly
with a hint, no silent degradation.

Mirror path (one-way DAG -> task board): params/inputs.blocked_by passes through
with teams.add_task; DAG artifact references pass through via artifact_refs and
attach to the task (artifact_channel ref, never full text).

Failure semantics: node failure never writes back to the task board — board state
is maintained by the kernel state machine, DAG failure does not pollute team state;
on timeout the task_id is kept for manual lookup (same detach semantics as studio_run).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List

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

# product-level call (roadmap §6): team_call drives agent-studio team members
# via teams.* JSON-RPC — the members/tasks live in the agent-studio daemon.
# In-process kernel TeamRuntime would change what executes (cowork-side
# subagents instead of studio members) — divergence documented in
# orchestrator/runner_pool.py (issue #494 Part B).
_DEFAULT_BASE_URL = "http://127.0.0.1:11442"

# aligned with studio_run's terminal-state semantics (board done/failed are terminal)
_TASK_TERMINAL_STATUSES = frozenset({"done", "failed"})

_POLL_INTERVAL = 2.0
_POLL_INTERVAL_MIN = 0.05
_POLL_INTERVAL_MAX = 30.0

# T3-missing signature: JSON-RPC -32601 (deny-by-default allowlist without teams.*)
_METHOD_NOT_FOUND = -32601


def _get_base_url() -> str:
    return os.environ.get("FUSION_AGENT_STUDIO_URL", _DEFAULT_BASE_URL).rstrip("/")


def _build_headers() -> Dict[str, str]:
    # upstream auth optional: locally trusted when no key configured, x-api-key header when set (same as studio_node)
    key = os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["x-api-key"] = key
    return headers


def _clamp_poll_interval(value: float) -> float:
    if value < _POLL_INTERVAL_MIN:
        return _POLL_INTERVAL_MIN
    if value > _POLL_INTERVAL_MAX:
        return _POLL_INTERVAL_MAX
    return value


async def _rpc_call(client: httpx.AsyncClient, method: str, params: Dict[str, Any]) -> Any:
    # agent-studio rpc_bridge: POST /v1/rpc, JSON-RPC 2.0 envelope, deny-by-default allowlist
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = await client.post("/v1/rpc", json=payload)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        code = err.get("code")
        message = str(err.get("message", ""))
        if code == _METHOD_NOT_FOUND:
            raise _TeamsMethodUnavailable(f"{method}: {message}")
        raise _TeamsRpcError(f"{method}: {message or code}")
    return (body or {}).get("result")


class _TeamsRpcError(Exception):
    pass


class _TeamsMethodUnavailable(Exception):
    pass


@register_node
class TeamCallNode(BaseNode):
    """Team call node — drive an agent-studio team member from within a cowork DAG.

    run mode (default): teams.add_task creates the task -> teams.run_member runs
    the member -> poll teams.status until the task reaches a terminal state;
    add_task mode only writes the task board (DAG->board mirror); run_member mode
    runs an existing task_id without creating a new task.
    """

    name = "team_call"
    # i18n: user-facing
    display_name = "团队调用"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = (
        "Drive an agent-studio team member from a DAG node (teams.add_task / "
        "teams.run_member / teams.status over JSON-RPC); task completion "
        " awaited on the board, DAG failure never mutates team state"
    )
    icon = "👥"
    # i18n: user-facing
    default_label = "团队调用"

    inputs = [
        # i18n: user-facing
        {"key": "team", "label": "团队名", "type": "string"},
        # i18n: user-facing
        {"key": "member", "label": "成员", "type": "string"},
        # i18n: user-facing
        {"key": "task_id", "label": "任务 ID", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "task_id", "label": "任务 ID", "type": "string"},
        # i18n: user-facing
        {"key": "status", "label": "任务状态", "type": "string"},
        # i18n: user-facing
        {"key": "summary", "label": "结果摘要", "type": "string"},
        # i18n: user-facing
        {"key": "artifact_ref", "label": "工件引用", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # i18n: user-facing
                "team": {"type": "string", "description": "团队名 (已 teams.register 的编制)"},
                # i18n: user-facing
                "member": {"type": "string", "description": "要驱动的成员名 (run/run_member 模式必填)"},
                "mode": {
                    "type": "string",
                    "enum": ["run", "add_task", "run_member"],
                    # i18n: user-facing
                    "description": (
                        # i18n: user-facing
                        "run = 建任务+跑成员+等终态 (缺省); add_task = 只在任务板建任务 "
                        # i18n: user-facing
                        "(DAG→板镜像); run_member = 跑既有 task_id 不建新任务"
                    ),
                    "default": "run",
                },
                # i18n: user-facing
                "task_id": {"type": "string", "description": "run_member 模式必填 — 既有任务 ID"},
                # i18n: user-facing
                "task_title": {"type": "string", "description": "run/add_task 模式的任务标题"},
                # i18n: user-facing
                "task_desc": {"type": "string", "description": "任务描述 (写入任务板)"},
                # i18n: user-facing
                "brief": {"type": "string", "description": "成员执行 brief (spawn_protocol 7 字段由上游补全)"},
                "blocked_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    # i18n: user-facing
                    "description": "DAG→任务板镜像: 上游任务 ID 列表随 add_task 透传 (blocked_by)",
                },
                "artifact_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    # i18n: user-facing
                    "description": "挂到任务的工件引用列表 (artifact_channel ref, 不走全文)",
                },
                "timeout": {
                    "type": "number",
                    # i18n: user-facing
                    "description": "等待任务终态的总超时秒数 (缺省 1800)",
                    "default": 1800.0,
                },
                # i18n: user-facing
                "poll_interval": {"type": "number", "description": "状态轮询间隔秒数", "default": 2.0},
            },
            "required": ["team"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        params = coerce_params(params or {}, self.get_params_schema())

        team = str(inputs.get("team", params.get("team", "")) or "").strip()
        member = str(inputs.get("member", params.get("member", "")) or "").strip()
        mode = str(params.get("mode") or "run").strip().lower()
        task_id = str(inputs.get("task_id", params.get("task_id", "")) or "").strip()
        task_title = str(params.get("task_title", "") or "")
        task_desc = str(params.get("task_desc", "") or "")
        brief = str(params.get("brief", "") or "")
        blocked_by = self._str_list(params.get("blocked_by")) or self._str_list(inputs.get("blocked_by"))
        artifact_refs = self._str_list(params.get("artifact_refs")) or self._str_list(inputs.get("artifact_refs"))
        timeout = float(params.get("timeout") or 1800.0)
        poll_interval = _clamp_poll_interval(float(params.get("poll_interval", _POLL_INTERVAL) or _POLL_INTERVAL))

        if not team:
            return NodeResult(status=NodeStatus.FAILED, error="missing required param team", summary="incomplete params")
        if mode in ("run", "run_member") and not member:
            return NodeResult(status=NodeStatus.FAILED, error=f"mode={mode} missing required param member", summary="incomplete params")
        if mode == "run_member" and not task_id:
            return NodeResult(
                status=NodeStatus.FAILED, error="mode=run_member missing required param task_id", summary="incomplete params"
            )
        if mode in ("run", "add_task") and not task_title:
            return NodeResult(
                status=NodeStatus.FAILED, error=f"mode={mode} missing required param task_title", summary="incomplete params"
            )

        logger.info(
            "TeamCallNode start team=%r member=%r mode=%s timeout=%.0fs",
            team,
            member,
            mode,
            timeout,
        )
        try:
            async with httpx.AsyncClient(
                base_url=_get_base_url(),
                headers=_build_headers(),
                timeout=60.0,
            ) as client:
                # lifecycle guard: reject inactive teams (paused/disbanded) outright, zero board writes
                state = await self._team_state(client, team)
                if state and str(state).lower() not in ("active", "", "unknown"):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"team {team} is {state} (inactive), execution refused and task board untouched",
                        summary="team inactive",
                    )

                if mode == "add_task":
                    created = await self._add_task(
                        client, team, task_title, task_desc, member, blocked_by, artifact_refs
                    )
                    new_id = str(created.get("task_id", "") or "")
                    logger.info("TeamCallNode add_task team=%r task_id=%s", team, new_id)
                    return NodeResult(
                        status=NodeStatus.SUCCESS,
                        data={
                            "task_id": new_id,
                            "status": str(created.get("status", "todo")),
                            "summary": "",
                            "artifact_ref": "",
                        },
                        summary=f"task board created task {new_id}",
                    )

                if mode == "run_member":
                    target_id = task_id
                else:
                    created = await self._add_task(
                        client, team, task_title, task_desc, member, blocked_by, artifact_refs
                    )
                    target_id = str(created.get("task_id", "") or "")
                    if not target_id:
                        return NodeResult(
                            status=NodeStatus.FAILED,
                            error=f"teams.add_task returned no task_id: {created}",
                            summary="task creation failed",
                        )

                await self._run_member(client, team, member, target_id, brief, artifact_refs)
                return await self._await_task(client, team, target_id, timeout, poll_interval)
        except _TeamsMethodUnavailable as e:
            logger.error("TeamCallNode teams.* dispatcher unavailable: %s", e)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(f"{e} — teams.* dispatcher not deployed (requires agent-studio T3 / 0.16.0), task board untouched"),
                summary="teams.* unavailable",
            )
        except _TeamsRpcError as e:
            logger.error("TeamCallNode teams RPC error: %s", e)
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="teams RPC error")
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:200] if e.response is not None else ""
            logger.error("TeamCallNode HTTP %s: %s", code, body)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"agent-studio HTTP {code}: {body}",
                summary="upstream HTTP error",
            )
        except Exception as e:
            logger.exception("TeamCallNode execution exception")
            return NodeResult(status=NodeStatus.FAILED, error=f"team call exception: {e}", summary="execution exception")

    @staticmethod
    def _str_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    async def _team_state(self, client: httpx.AsyncClient, team: str) -> str:
        try:
            status = await _rpc_call(client, "teams.status", {"team": team})
        except _TeamsMethodUnavailable:
            raise
        except _TeamsRpcError:
            # business errors like a missing team are surfaced by later calls — here we only care about run state
            return "unknown"
        if isinstance(status, dict):
            return str(status.get("state", "") or "")
        return "unknown"

    async def _add_task(
        self,
        client: httpx.AsyncClient,
        team: str,
        title: str,
        desc: str,
        member: str,
        blocked_by: List[str],
        artifact_refs: List[str],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"team": team, "title": title}
        if desc:
            payload["description"] = desc
        if member:
            payload["assignee"] = member
        # one-way mirror: DAG dependencies -> task board blocked_by (design doc §5.4)
        if blocked_by:
            payload["blocked_by"] = blocked_by
        if artifact_refs:
            payload["artifact_refs"] = artifact_refs
        result = await _rpc_call(client, "teams.add_task", payload)
        return result if isinstance(result, dict) else {}

    async def _run_member(
        self,
        client: httpx.AsyncClient,
        team: str,
        member: str,
        task_id: str,
        brief: str,
        artifact_refs: List[str],
    ) -> None:
        payload: Dict[str, Any] = {"team": team, "member": member, "task_id": task_id}
        if brief:
            payload["brief"] = brief
        if artifact_refs:
            payload["artifact_refs"] = artifact_refs
        await _rpc_call(client, "teams.run_member", payload)
        logger.info("TeamCallNode run_member team=%r member=%r task=%s", team, member, task_id)

    async def _await_task(
        self,
        client: httpx.AsyncClient,
        team: str,
        task_id: str,
        timeout: float,
        poll_interval: float,
    ) -> NodeResult:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            status = await _rpc_call(client, "teams.status", {"team": team})
            state = str((status or {}).get("state", "") or "")
            if state and state.lower() not in ("active", "unknown"):
                # running team turned inactive (paused/disbanded) — keep the task, no write-back
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"team {team} turned {state} while running, task {task_id} kept on the task board",
                    data={"task_id": task_id, "status": state, "summary": "", "artifact_ref": ""},
                    summary="team inactive",
                )
            tasks = (status or {}).get("tasks") if isinstance(status, dict) else None
            task = (tasks or {}).get(task_id) if isinstance(tasks, dict) else None
            task = task if isinstance(task, dict) else {}
            task_status = str(task.get("status", "") or "")
            if task_status in _TASK_TERMINAL_STATUSES:
                summary = str(task.get("summary", "") or task.get("result", "") or "")
                refs = self._str_list(task.get("artifact_refs"))
                artifact_ref = refs[0] if refs else str(task.get("artifact_ref", "") or "")
                if task_status == "done":
                    logger.info("TeamCallNode task %s done on team %r", task_id, team)
                    return NodeResult(
                        status=NodeStatus.SUCCESS,
                        data={
                            "task_id": task_id,
                            "status": task_status,
                            "summary": summary,
                            "artifact_ref": artifact_ref,
                        },
                        summary=f"task {task_id} completed ({summary[:60]})" if summary else f"task {task_id} completed",
                    )
                logger.warning("TeamCallNode task %s failed on team %r", task_id, team)
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"task {task_id} reached terminal failed on the task board: {summary or 'no detail'}",
                    data={"task_id": task_id, "status": task_status, "summary": summary, "artifact_ref": artifact_ref},
                    summary="task failed",
                )
            if time.monotonic() >= deadline:
                # same detach semantics as studio_run: do NOT withdraw the task on timeout — the
                # board task is kept and can be looked up / recovered manually by task_id
                logger.warning("TeamCallNode task %s terminal-state wait timed out (%.0fs), kept on the task board", task_id, timeout)
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=(
                        f"timed out waiting for task {task_id} terminal state ({timeout:.0f}s), task kept on the "
                        f"task board (team={team}); query/recover by task_id"
                    ),
                    data={
                        "task_id": task_id,
                        "status": task_status or "unknown",
                        "summary": "",
                        "artifact_ref": "",
                        "detached": True,
                    },
                    summary="terminal-state wait timed out (task kept)",
                )
            await asyncio.sleep(poll_interval)
