"""Enhanced task scheduler — Cron UI visual scheduling panel + task persistence + calendar view.

V0.2 features:
- Visual Cron scheduling panel (rendered via fusion://automation/)
- Task calendar view
- Task execution history statistics
- Task dependency orchestration
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .scheduler import TaskScheduler

logger = logging.getLogger(__name__)


@dataclass
class TaskExecution:
    """Task execution record."""

    task_id: str
    task_name: str
    workflow_id: str
    started_at: float
    completed_at: Optional[float] = None
    status: str = "running"
    error: str = ""
    tokens_used: int = 0
    duration_ms: float = 0.0


@dataclass
class TaskDependency:
    """Task dependency."""

    task_id: str
    depends_on: List[str] = field(default_factory=list)
    parallel_with: List[str] = field(default_factory=list)


class EnhancedScheduler:
    """Enhanced scheduler — adds visualization and management on top of TaskScheduler.

    Supports:
    - Task calendar view data
    - Task execution history
    - Task dependency orchestration
    - Statistics reports
    - JSON persistence
    """

    def __init__(self, scheduler: TaskScheduler, store_path: str = ""):
        self._scheduler = scheduler
        self._store_path = store_path or str(Path.home() / ".fusion-cowork" / "scheduler_history.json")
        self._executions: List[TaskExecution] = []
        self._dependencies: Dict[str, TaskDependency] = {}
        # R-1: execution record cap, drop oldest beyond it (old version only appended without trim, memory + persistence bloat)
        self._max_executions = 500
        self._load_history()

    # ── Task execution records ──

    def record_execution(
        self,
        task_id: str,
        task_name: str,
        workflow_id: str,
        status: str = "running",
        error: str = "",
        tokens_used: int = 0,
    ) -> TaskExecution:
        """Record a task execution."""
        execution = TaskExecution(
            task_id=task_id,
            task_name=task_name,
            workflow_id=workflow_id,
            started_at=time.time(),
            status=status,
            error=error,
            tokens_used=tokens_used,
        )
        self._executions.append(execution)
        # R-1: drop oldest beyond cap, prevents unbounded _executions growth
        if len(self._executions) > self._max_executions:
            self._executions = self._executions[-self._max_executions :]
            logger.debug(f"enhanced_scheduler execution records over cap, keeping latest {self._max_executions}")
        self._save_history()
        return execution

    def complete_execution(self, execution: TaskExecution, status: str = "completed") -> None:
        """Complete a task execution record."""
        execution.completed_at = time.time()
        execution.status = status
        execution.duration_ms = (execution.completed_at - execution.started_at) * 1000
        self._save_history()

    # ── Calendar view data ──

    def get_calendar_data(self, year: int, month: int) -> Dict[str, Any]:
        """Get calendar view data."""
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)

        start_ts = start.timestamp()
        end_ts = end.timestamp()

        month_executions = [e for e in self._executions if start_ts <= e.started_at < end_ts]

        # Group by day
        days = {}
        for e in month_executions:
            day = datetime.fromtimestamp(e.started_at).day
            if day not in days:
                days[day] = {"total": 0, "success": 0, "failed": 0, "tasks": []}
            days[day]["total"] += 1
            days[day]["tasks"].append(
                {
                    "name": e.task_name,
                    "status": e.status,
                    "time": datetime.fromtimestamp(e.started_at).strftime("%H:%M"),
                }
            )
            if e.status == "completed":
                days[day]["success"] += 1
            elif e.status == "failed":
                days[day]["failed"] += 1

        return {
            "year": year,
            "month": month,
            "days": days,
            "total": len(month_executions),
            "success": sum(1 for e in month_executions if e.status == "completed"),
            "failed": sum(1 for e in month_executions if e.status == "failed"),
        }

    # ── Statistics reports ──

    def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """Get scheduling statistics."""
        cutoff = time.time() - days * 86400
        recent = [e for e in self._executions if e.started_at > cutoff]

        total_tokens = sum(e.tokens_used for e in recent)
        avg_duration = sum(e.duration_ms for e in recent if e.duration_ms > 0) / max(
            len([e for e in recent if e.duration_ms > 0]), 1
        )

        # Group by task name
        by_task = {}
        for e in recent:
            by_task.setdefault(e.task_name, {"total": 0, "success": 0, "failed": 0})
            by_task[e.task_name]["total"] += 1
            if e.status == "completed":
                by_task[e.task_name]["success"] += 1
            elif e.status == "failed":
                by_task[e.task_name]["failed"] += 1

        return {
            "period_days": days,
            "total_executions": len(recent),
            "success": sum(1 for e in recent if e.status == "completed"),
            "failed": sum(1 for e in recent if e.status == "failed"),
            "running": sum(1 for e in recent if e.status == "running"),
            "total_tokens": total_tokens,
            "avg_duration_ms": round(avg_duration, 1),
            "by_task": by_task,
            "active_tasks": len(self._scheduler.list_active_tasks()),
        }

    # ── Task dependency orchestration ──

    def set_dependency(self, task_id: str, depends_on: List[str]) -> None:
        """Set task dependencies."""
        dep = self._dependencies.setdefault(task_id, TaskDependency(task_id=task_id))
        dep.depends_on = depends_on

    def get_dependency_graph(self) -> Dict[str, Any]:
        """Get dependency graph (for visualization)."""
        nodes = []
        edges = []

        for task_id, dep in self._dependencies.items():
            task = self._scheduler.get_task(task_id)
            nodes.append(
                {
                    "id": task_id,
                    "label": task.name if task else task_id,
                    "status": task.status.value if task else "unknown",
                }
            )
            for parent in dep.depends_on:
                edges.append({"from": parent, "to": task_id})

        return {"nodes": nodes, "edges": edges}

    # ── Persistence ──

    def _save_history(self) -> None:
        # E-4: atomic write — tmp+os.replace, avoids a half-written JSON on crash breaking _load_history parsing.
        path = Path(self._store_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "task_id": e.task_id,
                "task_name": e.task_name,
                "workflow_id": e.workflow_id,
                "started_at": e.started_at,
                "completed_at": e.completed_at,
                "status": e.status,
                "error": e.error,
                "tokens_used": e.tokens_used,
                "duration_ms": e.duration_ms,
            }
            for e in self._executions[-1000:]  # keep latest 1000
        ]
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            logger.exception(f"Failed atomic write of scheduling history: {path}")
            raise

    def _load_history(self) -> None:
        """Load execution history."""
        path = Path(self._store_path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._executions = [TaskExecution(**item) for item in data]
            except Exception as e:
                logger.warning(f"Failed to load scheduling history: {e}")

    # ── Cron expression helpers ──

    @staticmethod
    def cron_description(expression: str) -> str:
        """Convert a Cron expression into a human-readable description."""
        parts = expression.strip().split()
        if len(parts) != 5:
            return expression

        minute, hour, day, month, weekday = parts

        def _safe_int(field_value: str) -> Optional[int]:
            # R-5b: old version crashed with ValueError on int(weekday)/int(hour) for */2, 1,3, 1-5.
            # Parse only plain integer fields; complex expressions fall back verbatim (no crash).
            try:
                return int(field_value)
            except ValueError:
                return None

        desc = []
        if weekday != "*":
            # i18n: user-facing
            days = ["日", "一", "二", "三", "四", "五", "六"]
            wd = _safe_int(weekday)
            if wd is not None and 0 <= wd <= 6:
                # i18n: user-facing
                desc.append(f"每周{days[wd]}")
            else:
                # i18n: user-facing
                desc.append(f"每周{weekday}")
        if hour != "*":
            h = _safe_int(hour)
            if h is not None:
                # i18n: user-facing
                desc.append(f"每{hour}小时" if hour == "*/1" else f"{h:02d}:{minute.zfill(2)}")
            else:
                desc.append(f"{hour}:{minute.zfill(2)}")
        if day != "*":
            # i18n: user-facing
            desc.append(f"每月第{day}天")
        if minute == "0" and hour != "*":
            # i18n: user-facing
            return " ".join(desc) if desc else "每小时"

        return " ".join(desc) if desc else expression

    @staticmethod
    def common_cron_expressions() -> List[Dict[str, str]]:
        """Return a list of common Cron expressions."""
        return [
            # i18n: user-facing
            {"expr": "0 9 * * *", "label": "每天上午 9 点", "desc": "每日 09:00"},
            # i18n: user-facing
            {"expr": "0 21 * * *", "label": "每天晚上 9 点", "desc": "每日 21:00"},
            # i18n: user-facing
            {"expr": "0 */2 * * *", "label": "每 2 小时", "desc": "每 2 小时"},
            # i18n: user-facing
            {"expr": "0 0 * * 1", "label": "每周一凌晨", "desc": "每周一 00:00"},
            # i18n: user-facing
            {"expr": "0 0 1 * *", "label": "每月 1 号", "desc": "每月 1 日 00:00"},
            # i18n: user-facing
            {"expr": "*/30 * * * *", "label": "每 30 分钟", "desc": "每 30 分钟"},
            # i18n: user-facing
            {"expr": "0 9,21 * * *", "label": "每天早晚 9 点", "desc": "每日 09:00 和 21:00"},
        ]
