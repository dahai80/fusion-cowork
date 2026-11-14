"""Fusion 生态互通节点 — 通过 agent-studio teams.* RPC 驱动团队成员 (issue #488)。

设计边界 (fusion-team-architecture-2026-11-14.md §5.4): DAG 编排留在 cowork,
节点 handler 只是"一次 team 调用" — 调 agent-studio 的 teams.add_task /
teams.run_member / teams.status (JSON-RPC 2.0, POST /v1/rpc, 与 subagent.*
同级鉴权), 不把 team 逻辑搬进 cowork 引擎。T3 (agent-studio teams.* dispatcher)
未部署时节点响亮失败并给出提示, 不静默降级。

镜像路径 (单向 DAG→任务板): params/inputs.blocked_by 随 teams.add_task 透传;
DAG 工件引用经 artifact_refs 透传挂到任务 (artifact_channel ref, 不走全文)。

失败语义: 节点失败不回写任务板 — 板上状态由内核状态机维护, DAG 失败不污染
team 状态; 超时保留 task_id 供人工查询 (与 studio_run detach 语义一致)。
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

_DEFAULT_BASE_URL = "http://127.0.0.1:11442"

# 对齐 studio_run 的终态语义 (任务板 done/failed 即终态)
_TASK_TERMINAL_STATUSES = frozenset({"done", "failed"})

_POLL_INTERVAL = 2.0
_POLL_INTERVAL_MIN = 0.05
_POLL_INTERVAL_MAX = 30.0

# T3 缺失特征: JSON-RPC -32601 (deny-by-default allowlist 未含 teams.*)
_METHOD_NOT_FOUND = -32601


def _get_base_url() -> str:
    return os.environ.get("FUSION_AGENT_STUDIO_URL", _DEFAULT_BASE_URL).rstrip("/")


def _build_headers() -> Dict[str, str]:
    # 上游鉴权可选: 未配置 key 时本地受信放行, 有则 x-api-key 携带 (与 studio_node 一致)
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
    # agent-studio rpc_bridge: POST /v1/rpc, JSON-RPC 2.0 信封, deny-by-default allowlist
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
    """团队调用节点 — cowork DAG 内驱动 agent-studio team 成员执行任务。

    run 模式 (缺省): teams.add_task 建任务 → teams.run_member 跑成员 →
    轮询 teams.status 至任务终态; add_task 模式只写任务板 (DAG→板镜像);
    run_member 模式跑既有 task_id 不建新任务。
    """

    name = "team_call"
    display_name = "团队调用"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = (
        "Drive an agent-studio team member from a DAG node (teams.add_task / "
        "teams.run_member / teams.status over JSON-RPC); task completion "
        " awaited on the board, DAG failure never mutates team state"
    )
    icon = "👥"
    default_label = "团队调用"

    inputs = [
        {"key": "team", "label": "团队名", "type": "string"},
        {"key": "member", "label": "成员", "type": "string"},
        {"key": "task_id", "label": "任务 ID", "type": "string"},
    ]
    outputs = [
        {"key": "task_id", "label": "任务 ID", "type": "string"},
        {"key": "status", "label": "任务状态", "type": "string"},
        {"key": "summary", "label": "结果摘要", "type": "string"},
        {"key": "artifact_ref", "label": "工件引用", "type": "string"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "团队名 (已 teams.register 的编制)"},
                "member": {"type": "string", "description": "要驱动的成员名 (run/run_member 模式必填)"},
                "mode": {
                    "type": "string",
                    "enum": ["run", "add_task", "run_member"],
                    "description": (
                        "run = 建任务+跑成员+等终态 (缺省); add_task = 只在任务板建任务 "
                        "(DAG→板镜像); run_member = 跑既有 task_id 不建新任务"
                    ),
                    "default": "run",
                },
                "task_id": {"type": "string", "description": "run_member 模式必填 — 既有任务 ID"},
                "task_title": {"type": "string", "description": "run/add_task 模式的任务标题"},
                "task_desc": {"type": "string", "description": "任务描述 (写入任务板)"},
                "brief": {"type": "string", "description": "成员执行 brief (spawn_protocol 7 字段由上游补全)"},
                "blocked_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "DAG→任务板镜像: 上游任务 ID 列表随 add_task 透传 (blocked_by)",
                },
                "artifact_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "挂到任务的工件引用列表 (artifact_channel ref, 不走全文)",
                },
                "timeout": {
                    "type": "number",
                    "description": "等待任务终态的总超时秒数 (缺省 1800)",
                    "default": 1800.0,
                },
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
            return NodeResult(status=NodeStatus.FAILED, error="缺少必填参数 team", summary="参数不完整")
        if mode in ("run", "run_member") and not member:
            return NodeResult(status=NodeStatus.FAILED, error=f"mode={mode} 缺少必填参数 member", summary="参数不完整")
        if mode == "run_member" and not task_id:
            return NodeResult(
                status=NodeStatus.FAILED, error="mode=run_member 缺少必填参数 task_id", summary="参数不完整"
            )
        if mode in ("run", "add_task") and not task_title:
            return NodeResult(
                status=NodeStatus.FAILED, error=f"mode={mode} 缺少必填参数 task_title", summary="参数不完整"
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
                # 生命周期守卫: 非活跃团队 (paused/disbanded) 直接拒绝, 零板写入
                state = await self._team_state(client, team)
                if state and str(state).lower() not in ("active", "", "unknown"):
                    return NodeResult(
                        status=NodeStatus.FAILED,
                        error=f"团队 {team} 状态为 {state} (非活跃), 拒绝执行且未改动任务板",
                        summary="团队非活跃",
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
                        summary=f"任务板已建任务 {new_id}",
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
                            error=f"teams.add_task 未返回 task_id: {created}",
                            summary="建任务失败",
                        )

                await self._run_member(client, team, member, target_id, brief, artifact_refs)
                return await self._await_task(client, team, target_id, timeout, poll_interval)
        except _TeamsMethodUnavailable as e:
            logger.error("TeamCallNode teams.* dispatcher unavailable: %s", e)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(f"{e} — teams.* dispatcher 未部署 (需 agent-studio T3 / 0.16.0), 任务板未被改动"),
                summary="teams.* 不可用",
            )
        except _TeamsRpcError as e:
            logger.error("TeamCallNode teams RPC error: %s", e)
            return NodeResult(status=NodeStatus.FAILED, error=str(e), summary="teams RPC 错误")
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:200] if e.response is not None else ""
            logger.error("TeamCallNode HTTP %s: %s", code, body)
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"agent-studio HTTP {code}: {body}",
                summary="上游 HTTP 错误",
            )
        except Exception as e:
            logger.exception("TeamCallNode 执行异常")
            return NodeResult(status=NodeStatus.FAILED, error=f"团队调用异常: {e}", summary="执行异常")

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
            # 团队不存在等业务错误留给后续调用报 — 这里只关心运行态
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
        # 单向镜像: DAG 依赖 → 任务板 blocked_by (设计文档 §5.4)
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
                # 运行中团队转非活跃 (paused/disbanded) — 保留任务, 不回写
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"团队 {team} 运行中转为 {state}, 任务 {task_id} 保留在任务板",
                    data={"task_id": task_id, "status": state, "summary": "", "artifact_ref": ""},
                    summary="团队非活跃",
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
                        summary=f"任务 {task_id} 已完成 ({summary[:60]})" if summary else f"任务 {task_id} 已完成",
                    )
                logger.warning("TeamCallNode task %s failed on team %r", task_id, team)
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"任务 {task_id} 在任务板终态 failed: {summary or '无详情'}",
                    data={"task_id": task_id, "status": task_status, "summary": summary, "artifact_ref": artifact_ref},
                    summary="任务失败",
                )
            if time.monotonic() >= deadline:
                # 与 studio_run detach 语义一致: 超时不撤任务 — 板上任务保留,
                # 凭 task_id 可人工查询/回收
                logger.warning("TeamCallNode task %s 等待终态超时 (%.0fs), 保留在任务板", task_id, timeout)
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=(
                        f"等待任务 {task_id} 终态超时 ({timeout:.0f}s), 任务已保留在任务板 "
                        f"(team={team}), 可凭 task_id 查询/回收"
                    ),
                    data={
                        "task_id": task_id,
                        "status": task_status or "unknown",
                        "summary": "",
                        "artifact_ref": "",
                        "detached": True,
                    },
                    summary="等待任务终态超时 (任务保留)",
                )
            await asyncio.sleep(poll_interval)
