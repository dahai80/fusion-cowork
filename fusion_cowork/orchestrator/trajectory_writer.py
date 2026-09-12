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

_RETROSPECTIVE_DIR = "retrospectives"


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


def write_plan_retrospective(plan, results: Dict[str, Any], elapsed: float) -> str:
    """Write one retrospective line for a terminal plan; returns the path."""
    failed = [t.task_id for t in plan.tasks if t.status in ("failed", "skipped")]
    completed = [t.task_id for t in plan.tasks if t.status == "completed"]
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


def list_plan_retrospectives(limit: int = 20, trajectory_dir: str | None = None) -> list:
    """Read recent plan_retrospective events from the trajectory jsonl pool
    (newest first). Best-effort: unreadable/corrupt files are skipped."""
    from ..trajectory.recorder import DEFAULT_TRAJECTORY_DIR

    base = Path(trajectory_dir or DEFAULT_TRAJECTORY_DIR)
    if not base.is_dir():
        return []
    rows = []
    for f in base.glob("*.jsonl"):
        try:
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
                    rows.append(
                        {
                            "plan_id": evt.get("execution_id") or (evt.get("data") or {}).get("plan_id", ""),
                            "workflow_name": evt.get("workflow_name", ""),
                            "status": evt.get("status", ""),
                            "ts": evt.get("ts", 0),
                            "task_count": (evt.get("data") or {}).get("task_count", 0),
                            "failed_tasks": (evt.get("data") or {}).get("failed_tasks", []),
                        }
                    )
        except OSError:
            continue
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[: max(1, limit)]
