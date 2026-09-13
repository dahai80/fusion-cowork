"""Plan retrospective writer (audit 方案二③).

Appends a plan terminal snapshot to the trajectory jsonl so every
orchestrated run is reviewable afterwards: task tree with parent links,
per-task status/error/acceptance verdict, and the honest plan outcome.
Reuses TrajectoryWriter (zero new storage infrastructure).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

from ..trajectory.recorder import TrajectoryEvent, TrajectoryWriter

logger = logging.getLogger(__name__)


def _task_snapshot(task) -> Dict[str, Any]:
    return {
        "task_id": task.task_id,
        "agent_id": task.agent_id,
        "parent_task": task.parent_task,
        "description": task.description[:200],
        "status": task.status,
        "error": task.error[:300] if task.error else "",
        "acceptance_status": task.acceptance_status,
        "acceptance_criteria": task.acceptance_criteria[:200],
        "acceptor": task.acceptor,
        "retry_count": task.retry_count,
        "elapsed": round(task.completed_at - task.started_at, 3) if task.completed_at and task.started_at else None,
    }


def _collect_side_effects(results: Dict[str, Any]) -> list:
    """方案五 (audit v3): extract file-ish side effects from executor results
    (output_path / file_path / path keys in result data) so the retrospective
    doubles as a delivery note listing WHAT the run touched on disk."""
    effects = []
    keys = ("output_path", "output_file", "file_path", "saved_path", "path")
    for tid, r in (results or {}).items():
        if not isinstance(r, dict):
            continue
        data = r.get("data") if isinstance(r.get("data"), dict) else r
        for k in keys:
            v = data.get(k)
            if isinstance(v, str) and v and "/" in v and v not in effects:
                effects.append(v)
    return effects


def write_plan_retrospective(plan, results: Dict[str, Any], elapsed: float) -> str:
    """Write one retrospective line for a terminal plan; returns the path."""
    failed = [t.task_id for t in plan.tasks if t.status in ("failed", "skipped")]
    completed = [t.task_id for t in plan.tasks if t.status == "completed"]
    # 方案五 (audit v3): delivery-note fields — per-task timings, disk side
    # effects, and the superseded-retry history explaining plan churn.
    task_timings = {
        t.task_id: round(t.completed_at - t.started_at, 3) for t in plan.tasks if t.completed_at and t.started_at
    }
    evt = TrajectoryEvent(
        ts=time.time(),
        event="plan_retrospective",
        execution_id=plan.plan_id,
        workflow_id=plan.plan_id,
        workflow_name=plan.workflow_name,
        status=plan.status,
        is_error=plan.status in ("failed", "partial"),
        data={
            "plan_id": plan.plan_id,
            "workflow_name": plan.workflow_name,
            "plan_status": plan.status,
            "elapsed": round(elapsed, 3),
            "task_count": len(plan.tasks),
            "completed_tasks": completed,
            "failed_tasks": failed,
            "dependencies": plan.dependencies,
            "tasks": [_task_snapshot(t) for t in plan.tasks],
            "task_timings": task_timings,
            "side_effects": _collect_side_effects(results),
            "superseded_history": list(getattr(plan, "superseded_history", []) or []),
            "results_summary": {
                tid: {
                    "status": (r or {}).get("status", "") if isinstance(r, dict) else "",
                    "error": str((r or {}).get("error", ""))[:200] if isinstance(r, dict) else str(r)[:200],
                }
                for tid, r in results.items()
            },
        },
    )
    path = TrajectoryWriter().write(evt)
    logger.info(f"plan retrospective written: {path} plan={plan.plan_id} status={plan.status}")
    return path


def list_plan_retrospectives(
    limit: int = 20,
    trajectory_dir: str | None = None,
    after_ts: float = 0.0,
) -> list:
    """Read recent plan_retrospective events from the trajectory jsonl pool
    (newest first). Best-effort: unreadable/corrupt files are skipped.

    v2 P2: `after_ts` enables incremental polling — files whose mtime is
    older than after_ts are skipped entirely instead of re-parsing every
    jsonl line on each dashboard refresh."""
    from ..trajectory.recorder import DEFAULT_TRAJECTORY_DIR

    base = Path(trajectory_dir or DEFAULT_TRAJECTORY_DIR)
    if not base.is_dir():
        return []
    rows = []
    for f in base.glob("*.jsonl"):
        try:
            # v2 P2: skip cold files wholesale — a retrospective in this file
            # can only be newer than the file's mtime, so files untouched
            # since after_ts cannot contain new events.
            if after_ts and f.stat().st_mtime <= after_ts:
                continue
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("event") != "plan_retrospective":
                        continue
                    ts = evt.get("ts", 0)
                    if after_ts and ts <= after_ts:
                        continue
                    rows.append(
                        {
                            "plan_id": evt.get("execution_id") or (evt.get("data") or {}).get("plan_id", ""),
                            "workflow_name": evt.get("workflow_name", ""),
                            "status": evt.get("status", ""),
                            "ts": ts,
                            "task_count": (evt.get("data") or {}).get("task_count", 0),
                            "failed_tasks": (evt.get("data") or {}).get("failed_tasks", []),
                            # 方案五 (audit v3): delivery-note fields for the GUI
                            "task_timings": (evt.get("data") or {}).get("task_timings", {}),
                            "side_effects": (evt.get("data") or {}).get("side_effects", []),
                            "superseded_history": (evt.get("data") or {}).get("superseded_history", []),
                        }
                    )
        except OSError:
            continue
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[: max(1, limit)]


def write_task_step(
    space_id: str,
    agent_id: str,
    step: str,
    content: str,
) -> None:
    """方案二 (audit v3): persist one agent execution intermediate step
    (tool output, error, retry trace) to the trajectory jsonl. Best-effort —
    a write failure must never break the relay chain."""
    try:
        evt = TrajectoryEvent(
            ts=time.time(),
            event="task_step",
            execution_id=f"space:{space_id}",
            workflow_id=space_id,
            workflow_name="relay",
            status="running",
            is_error=False,
            data={"space_id": space_id, "agent_id": agent_id, "step": step, "content": content[:2000]},
        )
        TrajectoryWriter().write(evt)
    except Exception as e:
        logger.debug(f"write_task_step failed (best-effort): {e}")


def read_task_steps(
    space_id: str,
    limit: int = 10,
    after_ts: float = 0.0,
) -> list:
    """方案二 (audit v3): read recent task_step events for a space (oldest
    first) so relay context can include intermediate outputs that never went
    through the chat table. Best-effort; returns [] on any failure."""
    from ..trajectory.recorder import DEFAULT_TRAJECTORY_DIR

    base = Path(DEFAULT_TRAJECTORY_DIR)
    if not base.is_dir():
        return []
    rows = []
    for f in base.glob("*.jsonl"):
        try:
            if after_ts and f.stat().st_mtime <= after_ts:
                continue
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("event") != "task_step":
                        continue
                    ts = evt.get("ts", 0)
                    if ts <= after_ts:
                        continue
                    data = evt.get("data") or {}
                    if data.get("space_id") != space_id:
                        continue
                    rows.append(
                        {
                            "ts": ts,
                            "agent_id": data.get("agent_id", ""),
                            "step": data.get("step", ""),
                            "content": data.get("content", ""),
                        }
                    )
        except OSError:
            continue
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows[-max(1, limit) :]
