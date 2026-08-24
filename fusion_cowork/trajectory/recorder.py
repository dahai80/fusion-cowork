"""D1 轨迹飞轮 — 轨迹记录器。

设计：
- TrajectoryRecorder 注册到 HookManager，监听 14 种 HookEvent
- 每个事件转为 TrajectoryEvent 结构化记录，append 到汇聚目录 jsonl
- WORKFLOW_END 时回填 sessions.steps_snapshot（含完整步骤快照）
- NODE_ERROR 单独标注 is_error，供 D1 清洗流水线消费

汇聚目录: ~/.fusion/trajectories/cowork/<session_id>.jsonl
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..engine.hooks import HookContext, HookEvent, HookManager

logger = logging.getLogger(__name__)

DEFAULT_TRAJECTORY_DIR = str(Path.home() / ".fusion" / "trajectories" / "cowork")


def _json_safe(obj: Any) -> Any:
    """递归将对象转为 JSON 可序列化形式（枚举取 value，其余转 str）。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if hasattr(obj, "value"):
        return obj.value
    if hasattr(obj, "__dict__"):
        try:
            return _json_safe(vars(obj))
        except Exception:
            return str(obj)
    return str(obj)


@dataclass
class TrajectoryEvent:
    ts: float
    event: str
    execution_id: str = ""
    workflow_id: str = ""
    workflow_name: str = ""
    session_id: str = ""
    node_id: str = ""
    node_name: str = ""
    status: str = ""
    error: Optional[str] = None
    is_error: bool = False
    data: Dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class TrajectoryWriter:
    """轨迹写入器 — 按会话 append 到汇聚目录 jsonl 文件。"""

    def __init__(self, trajectory_dir: str = DEFAULT_TRAJECTORY_DIR):
        self._dir = Path(trajectory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"TrajectoryWriter 初始化: {self._dir}")

    def write(self, evt: TrajectoryEvent) -> str:
        session_id = evt.session_id or "no_session"
        path = self._dir / f"{session_id}.jsonl"
        line = evt.to_jsonl()
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.debug(f"轨迹写入: {path.name} event={evt.event} node={evt.node_id}")
        return str(path)

    def list_trajectories(self) -> List[str]:
        if not self._dir.exists():
            return []
        return sorted(p.name for p in self._dir.glob("*.jsonl"))


class TrajectoryRecorder:
    """轨迹记录器 — 注册到 HookManager，持久化结构化执行轨迹。

    用法：
        recorder = TrajectoryRecorder(hook_manager, session_store=store, session_id=sess.id)
        recorder.attach()
    """

    def __init__(
        self,
        hook_manager: HookManager,
        session_store: Any = None,
        session_id: str = "",
        trajectory_dir: str = DEFAULT_TRAJECTORY_DIR,
    ):
        self._hook_manager = hook_manager
        self._session_store = session_store
        self._session_id = session_id
        self._writer = TrajectoryWriter(trajectory_dir)
        # CR-12: 按 execution_id 命名空间, 防共享 HookManager 上并发执行互相覆盖快照/元数据
        self._execs: Dict[str, Dict[str, Any]] = {}
        self._attached = False

    def attach(self) -> None:
        if self._attached:
            logger.warning("TrajectoryRecorder 已 attach，跳过重复注册")
            return
        self._do_attach()

    def bind_session(self, session_id: str) -> None:
        if not session_id:
            return
        if self._session_id and self._session_id != session_id:
            logger.warning(f"TrajectoryRecorder 切换 session: {self._session_id} -> {session_id}")
        self._session_id = session_id
        logger.debug(f"TrajectoryRecorder 绑定 session: {session_id}")

    def _do_attach(self) -> None:
        events = [
            HookEvent.WORKFLOW_START,
            HookEvent.WORKFLOW_END,
            HookEvent.WORKFLOW_CANCEL,
            HookEvent.PRE_NODE_EXECUTE,
            HookEvent.POST_NODE_EXECUTE,
            HookEvent.NODE_ERROR,
            HookEvent.SESSION_START,
            HookEvent.SESSION_END,
            HookEvent.AGENT_START,
            HookEvent.AGENT_STOP,
            HookEvent.PERMISSION_REQUEST,
            HookEvent.CONFIG_CHANGE,
            HookEvent.NOTIFICATION,
            HookEvent.PRE_COMPACT,
        ]
        for evt in events:
            self._hook_manager.register(evt, self._make_handler(evt))
        self._attached = True
        logger.info(f"TrajectoryRecorder 已注册 {len(events)} 个 Hook 处理器, session={self._session_id}")

    def _make_handler(self, evt: HookEvent):
        async def handler(ctx: HookContext) -> None:
            await self._on_hook(evt, ctx)

        handler.__name__ = f"_trajectory_{evt.value}"
        return handler

    async def _on_hook(self, evt: HookEvent, ctx: HookContext) -> None:
        data = ctx.data or {}
        # CR-12: 每个执行独立状态槽, 按 execution_id 隔离 (并发执行互不覆盖)
        exec_id = data.get("execution_id", "")
        state = self._execs.get(exec_id)
        if evt == HookEvent.WORKFLOW_START:
            state = {
                "execution_id": exec_id,
                "workflow_id": data.get("workflow_id", ""),
                "workflow_name": data.get("workflow_name", ""),
                "steps_snapshot": [],
            }
            self._execs[exec_id] = state
        if state is None:
            # CR-12: exec_id 缺失 (如手动 fire POST_NODE 未带 execution_id) 时,
            # 回退唯一活跃执行槽 (单并发常见), 避免步骤落入孤立临时槽永不回填
            if not exec_id and len(self._execs) == 1:
                state = next(iter(self._execs.values()))
                exec_id = state["execution_id"]
            else:
                # 非 workflow 事件或未知 exec_id — 用临时槽, 不持久化回 _execs
                state = {
                    "execution_id": exec_id,
                    "workflow_id": "",
                    "workflow_name": "",
                    "steps_snapshot": [],
                }
        node_id = data.get("node_id", "")
        node_name = data.get("node_name", "")
        status = data.get("status", "")
        error = data.get("error")
        execution_time = data.get("execution_time", 0.0)
        summary = data.get("summary", "")
        result = data.get("result")
        if result is not None and hasattr(result, "status"):
            status = getattr(result.status, "value", str(result.status))
            error = getattr(result, "error", error)
            summary = getattr(result, "summary", summary)
        if evt == HookEvent.POST_NODE_EXECUTE and node_id:
            state["steps_snapshot"].append(
                {
                    "node_id": node_id,
                    "node_name": node_name,
                    "status": status,
                    "execution_time": execution_time,
                    "error": error,
                    "summary": summary,
                }
            )
        is_error = evt == HookEvent.NODE_ERROR
        safe_data = _json_safe(data)
        traj = TrajectoryEvent(
            ts=time.time(),
            event=evt.value,
            execution_id=state["execution_id"],
            workflow_id=state["workflow_id"],
            workflow_name=state["workflow_name"],
            session_id=self._session_id,
            node_id=node_id,
            node_name=node_name,
            status=status if isinstance(status, str) else str(status),
            error=error,
            is_error=is_error,
            data=safe_data,
        )
        try:
            self._writer.write(traj)
        except Exception as e:
            logger.error(f"轨迹写入失败: {evt.value} -> {e}")
        if evt == HookEvent.WORKFLOW_END:
            if self._session_store and self._session_id:
                try:
                    self._session_store.update_steps(self._session_id, state["steps_snapshot"])
                    logger.info(
                        f"steps_snapshot 回填: session={self._session_id} "
                        f"exec={exec_id} steps={len(state['steps_snapshot'])}"
                    )
                except Exception as e:
                    logger.error(f"steps_snapshot 回填失败: {e}")
            # CR-12: 执行结束无条件清理状态槽防内存增长 (与 session_store 无关)
            self._execs.pop(exec_id, None)

    @property
    def steps_snapshot(self) -> List[Dict[str, Any]]:
        # CR-12: 无 exec_id 上下文时返回最后已知执行快照 (兼容旧调用)
        if self._execs:
            last = next(reversed(self._execs.values()))
            return list(last["steps_snapshot"])
        return []
