"""D1 trajectory flywheel - trajectory recorder.

Design (issue #520, K3 closeout):
- TrajectoryRecorder registers with HookManager, listening to 14 HookEvents
- Each event maps through kernel_record() to the kernel `fusion_harness.trajectory`
  TrajectoryRecord (the only schema; no local trajectory schema is kept), appended as jsonl
- cowork semantic event names are preserved in payload.legacy_event, matching the
  kernel reader's legacy adaptation contract; legacy-format files are normalized
  by the kernel TrajectoryReader
- fusion-harness is a mandatory dependency (#521 drop-fallback ruling): a missing
  package is an import-time startup error (with actionable install guidance);
  there is no degraded mode
- On WORKFLOW_END, sessions.steps_snapshot is backfilled (full step snapshot)
- NODE_ERROR is flagged with is_error for the D1 cleaning pipeline

Aggregation directory: ~/.fusion/trajectories/cowork/<session_id>.jsonl
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..engine._harness_guard import harness_import_error
from ..engine.hooks import HookContext, HookEvent, HookManager
from ..observability.trace import get_trace_id

logger = logging.getLogger(__name__)

DEFAULT_TRAJECTORY_DIR = str(Path.home() / ".fusion" / "trajectories" / "cowork")

try:  # issue #520: kernel trajectory schema is the single source of truth
    from fusion_harness.trajectory import TrajectoryRecord
except ImportError as exc:  # issue #521: mandatory dependency, fail fast
    raise harness_import_error(exc) from exc

# cowork semantic event -> kernel normative event (mirrors the kernel reader's
# legacy table so writer and reader agree without a private import)
_NORMATIVE_EVENT_MAP = {
    "workflow_start": "start",
    "session_start": "start",
    "agent_start": "start",
    "workflow_end": "end",
    "workflow_cancel": "end",
    "session_end": "end",
    "agent_stop": "end",
    "node_error": "error",
    "pre_node_execute": "start",
    "post_node_execute": "tool_result",
    "permission_request": "tool_call",
    "config_change": "tool_result",
    "notification": "tool_result",
    "pre_compact": "tool_result",
    "directive": "tool_call",
    "plan_retrospective": "tool_result",
    "task_step": "tool_result",
    "task_acceptance": "tool_result",
}


def kernel_record(
    event: str,
    run_id: str,
    node_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    duration_ms: int = 0,
    status: str = "",
    ts_sec: float = 0.0,
) -> TrajectoryRecord:
    """Build one kernel TrajectoryRecord from a cowork semantic event.

    The semantic event name is preserved in payload.legacy_event; the record
    event is the nearest kernel normative event (unknown -> tool_result,
    matching the kernel reader's default mapping).
    """
    pl = dict(payload or {})
    pl["legacy_event"] = event
    ts = float(ts_sec or time.time())
    return TrajectoryRecord(
        run_id=str(run_id or "no_run"),
        node_id=str(node_id or "workflow"),
        ts=int(ts * 1000) if ts < 10**12 else int(ts),
        event=_NORMATIVE_EVENT_MAP.get(event, "tool_result"),
        payload=pl,
        duration_ms=int(duration_ms or 0),
        status=str(status or ""),
    )


def _json_safe(obj: Any) -> Any:
    """Recursively convert an object to a JSON-serializable form (enums take .value, rest become str)."""
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


class TrajectoryWriter:
    """Trajectory writer - appends per session (payload.session_id) to the aggregation directory jsonl."""

    def __init__(self, trajectory_dir: str = DEFAULT_TRAJECTORY_DIR):
        self._dir = Path(trajectory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"TrajectoryWriter initialized: {self._dir}")

    def write(self, record: TrajectoryRecord) -> str:
        session_id = str((record.payload or {}).get("session_id") or "no_session")
        path = self._dir / f"{session_id}.jsonl"
        line = record.to_json()
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # v4 scheme-1: size-based rotation + retention cleanup — the writer is
        # append-only and task_step made writes much more frequent; without
        # this a 7x24 deployment grows the pool until disk quota dies.
        # Best-effort: cleanup failures never break the actual write.
        self._rotate_if_oversized(path)
        self._cleanup_expired()
        logger.debug(f"trajectory write: {path.name} event={record.event} node={record.node_id}")
        return str(path)

    @staticmethod
    def _retention_days() -> int:
        try:
            return max(1, int(os.environ.get("FUSION_TRAJECTORY_RETENTION_DAYS", "30")))
        except ValueError:
            return 30

    @staticmethod
    def _max_file_bytes() -> int:
        try:
            return max(1, int(os.environ.get("FUSION_TRAJECTORY_MAX_MB", "200"))) * 1024 * 1024
        except ValueError:
            return 200 * 1024 * 1024

    def _rotate_if_oversized(self, path: Path) -> None:
        """Scheme-1: rename an over-limit jsonl aside (timestamped) so the
        active file stays bounded; the rotated file ages out via retention."""
        try:
            if not path.exists() or path.stat().st_size < self._max_file_bytes():
                return
            rotated = path.with_name(f"{path.stem}.{int(time.time())}.rotated.jsonl")
            path.rename(rotated)
            logger.info(f"trajectory rotated (size limit): {path.name} -> {rotated.name}")
        except OSError as e:
            logger.debug(f"trajectory rotation skipped: {e}")

    def _cleanup_expired(self) -> None:
        """Scheme-1: delete jsonl files older than the retention window.
        Throttled to once per hour per writer instance; readers are
        best-effort, so removed files degrade gracefully."""
        now = time.time()
        if now - getattr(self, "_last_cleanup", 0.0) < 3600:
            return
        self._last_cleanup = now
        cutoff = now - self._retention_days() * 86400
        try:
            for f in self._dir.glob("*.jsonl"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        logger.info(f"trajectory expired (>{self._retention_days()}d): {f.name}")
                except OSError:
                    continue
        except OSError as e:
            logger.debug(f"trajectory cleanup skipped: {e}")

    def list_trajectories(self) -> List[str]:
        if not self._dir.exists():
            return []
        return sorted(p.name for p in self._dir.glob("*.jsonl"))


class TrajectoryRecorder:
    """Trajectory recorder - registers with HookManager, persists structured execution trajectories.

    Usage:
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
        # CR-12: namespace by execution_id so concurrent executions on a shared
        # HookManager do not overwrite each other's snapshots/metadata
        self._execs: Dict[str, Dict[str, Any]] = {}
        self._attached = False

    def attach(self) -> None:
        if self._attached:
            logger.warning("TrajectoryRecorder already attached, skipping duplicate registration")
            return
        self._do_attach()

    def bind_session(self, session_id: str) -> None:
        if not session_id:
            return
        if self._session_id and self._session_id != session_id:
            logger.warning(f"TrajectoryRecorder switching session: {self._session_id} -> {session_id}")
        self._session_id = session_id
        logger.debug(f"TrajectoryRecorder bound session: {session_id}")

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
        logger.info(f"TrajectoryRecorder registered {len(events)} hook handlers, session={self._session_id}")

    def _make_handler(self, evt: HookEvent):
        async def handler(ctx: HookContext) -> None:
            await self._on_hook(evt, ctx)

        handler.__name__ = f"_trajectory_{evt.value}"
        return handler

    async def _on_hook(self, evt: HookEvent, ctx: HookContext) -> None:
        data = ctx.data or {}
        # CR-12: one state slot per execution, isolated by execution_id (concurrent runs never overwrite)
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
            # CR-12: when exec_id is missing (e.g. a manually fired POST_NODE without
            # execution_id), fall back to the single active execution slot (common in
            # single-concurrency) so steps never land in an orphan temp slot forever
            if not exec_id and len(self._execs) == 1:
                state = next(iter(self._execs.values()))
                exec_id = state["execution_id"]
            else:
                # non-workflow event or unknown exec_id — use a temp slot, do not persist back into _execs
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
        # issue #520: single kernel schema — semantic event name lands in
        # payload.legacy_event, everything cowork-specific rides in payload
        traj = kernel_record(
            event=evt.value,
            run_id=state["execution_id"] or self._session_id,
            node_id=node_id,
            payload={
                "execution_id": state["execution_id"],
                "workflow_id": state["workflow_id"],
                "workflow_name": state["workflow_name"],
                "session_id": self._session_id,
                "trace_id": get_trace_id(),
                "node_name": node_name,
                "error": error if isinstance(error, str) else ("" if error is None else str(error)),
                "is_error": is_error,
                "data": safe_data,
            },
            duration_ms=int(float(execution_time or 0) * 1000),
            status=status if isinstance(status, str) else str(status),
        )
        try:
            self._writer.write(traj)
        except Exception as e:
            logger.error(f"trajectory write failed: {evt.value} -> {e}")
        if evt == HookEvent.WORKFLOW_END:
            if self._session_store and self._session_id:
                try:
                    self._session_store.update_steps(self._session_id, state["steps_snapshot"])
                    logger.info(
                        f"steps_snapshot backfill: session={self._session_id} "
                        f"exec={exec_id} steps={len(state['steps_snapshot'])}"
                    )
                except Exception as e:
                    logger.error(f"steps_snapshot backfill failed: {e}")
            # CR-12: unconditionally clean up state slots on execution end to prevent memory growth (independent of session_store)
            self._execs.pop(exec_id, None)

    def record_directive(self, execution_id: str, directive: Dict[str, Any]) -> None:
        """W8/B1: persist run redirection directives as their own trajectory, consumed by the review/training pipeline."""
        state = self._execs.get(execution_id) or {}
        traj = kernel_record(
            event="directive",
            run_id=execution_id or self._session_id,
            payload={
                "workflow_id": state.get("workflow_id", ""),
                "workflow_name": state.get("workflow_name", ""),
                "session_id": self._session_id,
                "trace_id": get_trace_id(),
                "directive": _json_safe(dict(directive)),
            },
            status="queued",
        )
        try:
            self._writer.write(traj)
        except Exception as e:
            logger.error(f"directive trajectory write failed: exec={execution_id} -> {e}")
            raise
        logger.info(f"directive recorded to trajectory: exec={execution_id} action={directive.get('action', '')}")

    @property
    def steps_snapshot(self) -> List[Dict[str, Any]]:
        # CR-12: return the last known execution snapshot when no exec_id context (legacy-call compat)
        if self._execs:
            last = next(reversed(self._execs.values()))
            return list(last["steps_snapshot"])
        return []
