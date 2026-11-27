"""Plan retrospective writer (audit scheme 2, item 3).

Appends a plan terminal snapshot to the trajectory jsonl so every
orchestrated run is reviewable afterwards: task tree with parent links,
per-task status/error/acceptance verdict, and the honest plan outcome.
Reuses TrajectoryWriter (zero new storage infrastructure).

issue #520 (K3 closeout): records are kernel `fusion_harness.trajectory`
TrajectoryRecord instances — the semantic event name (plan_retrospective /
task_step / task_acceptance) is preserved in payload.legacy_event. Readers
accept both the kernel line shape and the pre-#520 legacy lines so old
jsonl pools stay queryable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from ..trajectory.recorder import DEFAULT_TRAJECTORY_DIR, TrajectoryWriter, kernel_record

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
    """Scheme 5 (audit v3): extract file-ish side effects from executor results
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
    # Scheme 5 (audit v3): delivery-note fields — per-task timings, disk side
    # effects, and the superseded-retry history explaining plan churn.
    task_timings = {
        t.task_id: round(t.completed_at - t.started_at, 3) for t in plan.tasks if t.completed_at and t.started_at
    }
    evt = kernel_record(
        event="plan_retrospective",
        run_id=plan.plan_id,
        node_id="plan",
        status=plan.status,
        payload={
            "plan_id": plan.plan_id,
            "workflow_name": plan.workflow_name,
            "plan_status": plan.status,
            "is_error": plan.status in ("failed", "partial"),
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


def _iter_lines(base: Path, after_ts: float):
    """Yield (ts_sec, raw_dict) for every jsonl line, both kernel-shaped and
    pre-#520 legacy lines; cold files skipped by mtime (v2 P2 polling)."""
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
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # kernel shape: ts in ms, semantic event in payload
                    if "payload" in raw and "run_id" in raw:
                        payload = raw.get("payload") or {}
                        ts = raw.get("ts", 0)
                        ts_sec = ts / 1000 if ts > 10**11 else ts
                        semantic_event = payload.get("legacy_event") or raw.get("event", "")
                        yield (
                            ts_sec,
                            {
                                "event": semantic_event,
                                "execution_id": raw.get("run_id", ""),
                                "workflow_name": payload.get("workflow_name", ""),
                                "status": raw.get("status", ""),
                                "is_error": bool(payload.get("is_error")),
                                "payload": payload,
                                "data": payload.get("data") or payload,
                            },
                        )
                    else:
                        # legacy line: ts already seconds, data/workflow_name top-level
                        yield raw.get("ts", 0), raw
        except OSError:
            continue


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
    base = Path(trajectory_dir or DEFAULT_TRAJECTORY_DIR)
    if not base.is_dir():
        return []
    rows = []
    for ts, evt in _iter_lines(base, after_ts):
        if evt.get("event") != "plan_retrospective":
            continue
        if after_ts and ts <= after_ts:
            continue
        data = evt.get("data") or {}
        rows.append(
            {
                "plan_id": evt.get("execution_id") or data.get("plan_id", ""),
                "workflow_name": evt.get("workflow_name", ""),
                "status": evt.get("status", ""),
                "ts": ts,
                "task_count": data.get("task_count", 0),
                "failed_tasks": data.get("failed_tasks", []),
                # Scheme 5 (audit v3): delivery-note fields for the GUI
                "task_timings": data.get("task_timings", {}),
                "side_effects": data.get("side_effects", []),
                "superseded_history": data.get("superseded_history", []),
            }
        )
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[: max(1, limit)]


def write_task_step(
    space_id: str,
    agent_id: str,
    step: str,
    content: str,
) -> None:
    """Scheme 2 (audit v3): persist one agent execution intermediate step
    (tool output, error, retry trace) to the trajectory jsonl. Best-effort —
    a write failure must never break the relay chain."""
    try:
        evt = kernel_record(
            event="task_step",
            run_id=f"space:{space_id}",
            node_id=f"agent:{agent_id}" if agent_id else "agent",
            status="running",
            payload={
                "space_id": space_id,
                "agent_id": agent_id,
                "step": step,
                "content": content[:2000],
                "workflow_name": "relay",
            },
        )
        TrajectoryWriter().write(evt)
    except Exception as e:
        logger.debug(f"write_task_step failed (best-effort): {e}")


def read_task_steps(
    space_id: str,
    limit: int = 10,
    after_ts: float = 0.0,
) -> list:
    """Scheme 2 (audit v3): read recent task_step events for a space (oldest
    first) so relay context can include intermediate outputs that never went
    through the chat table. Best-effort; returns [] on any failure."""
    base = Path(DEFAULT_TRAJECTORY_DIR)
    if not base.is_dir():
        return []
    rows = []
    for ts, evt in _iter_lines(base, after_ts):
        if evt.get("event") != "task_step":
            continue
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
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows[-max(1, limit) :]
