"""增强任务调度器 — Cron UI 可视化调度面板 + 任务持久化 + 日历视图。

V0.2 特性：
- 可视化 Cron 调度面板（通过 fusion://automation/ 渲染）
- 任务日历视图
- 任务执行历史统计
- 任务依赖关系编排
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .scheduler import TaskScheduler

logger = logging.getLogger(__name__)


@dataclass
class TaskExecution:
    """任务执行记录。"""

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
    """任务依赖关系。"""

    task_id: str
    depends_on: List[str] = field(default_factory=list)
    parallel_with: List[str] = field(default_factory=list)


class EnhancedScheduler:
    """增强调度器 — 在 TaskScheduler 基础上增加可视化和管理能力。

    支持：
    - 任务日历视图数据
    - 任务执行历史
    - 任务依赖编排
    - 统计报表
    - 持久化到 JSON
    """

    def __init__(self, scheduler: TaskScheduler, store_path: str = ""):
        self._scheduler = scheduler
        self._store_path = store_path or str(Path.home() / ".fusion-cowork" / "scheduler_history.json")
        self._executions: List[TaskExecution] = []
        self._dependencies: Dict[str, TaskDependency] = {}
        # R-1: 执行记录上限, 超出丢最旧 (旧版只 append 不 trim, 内存 + 持久化膨胀)
        self._max_executions = 500
        self._load_history()

    # ── 任务执行记录 ──

    def record_execution(
        self,
        task_id: str,
        task_name: str,
        workflow_id: str,
        status: str = "running",
        error: str = "",
        tokens_used: int = 0,
    ) -> TaskExecution:
        """记录任务执行。"""
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
        # R-1: 超上限丢最旧, 防 _executions 无界增长
        if len(self._executions) > self._max_executions:
            self._executions = self._executions[-self._max_executions :]
            logger.debug(f"enhanced_scheduler 执行记录超上限, 保留最近 {self._max_executions} 条")
        self._save_history()
        return execution

    def complete_execution(self, execution: TaskExecution, status: str = "completed") -> None:
        """完成任务执行记录。"""
        execution.completed_at = time.time()
        execution.status = status
        execution.duration_ms = (execution.completed_at - execution.started_at) * 1000
        self._save_history()

    # ── 日历视图数据 ──

    def get_calendar_data(self, year: int, month: int) -> Dict[str, Any]:
        """获取日历视图数据。"""
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)

        start_ts = start.timestamp()
        end_ts = end.timestamp()

        month_executions = [e for e in self._executions if start_ts <= e.started_at < end_ts]

        # 按日期分组
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

    # ── 统计报表 ──

    def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """获取调度统计。"""
        cutoff = time.time() - days * 86400
        recent = [e for e in self._executions if e.started_at > cutoff]

        total_tokens = sum(e.tokens_used for e in recent)
        avg_duration = sum(e.duration_ms for e in recent if e.duration_ms > 0) / max(
            len([e for e in recent if e.duration_ms > 0]), 1
        )

        # 按任务名分组
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

    # ── 任务依赖编排 ──

    def set_dependency(self, task_id: str, depends_on: List[str]) -> None:
        """设置任务依赖。"""
        dep = self._dependencies.setdefault(task_id, TaskDependency(task_id=task_id))
        dep.depends_on = depends_on

    def get_dependency_graph(self) -> Dict[str, Any]:
        """获取依赖关系图（用于可视化）。"""
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

    # ── 持久化 ──

    def _save_history(self) -> None:
        """保存执行历史到 JSON。"""
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
            for e in self._executions[-1000:]  # 保留最近 1000 条
        ]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_history(self) -> None:
        """加载执行历史。"""
        path = Path(self._store_path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._executions = [TaskExecution(**item) for item in data]
            except Exception as e:
                logger.warning(f"加载调度历史失败: {e}")

    # ── Cron 表达式助手 ──

    @staticmethod
    def cron_description(expression: str) -> str:
        """将 Cron 表达式转换为人类可读描述。"""
        parts = expression.strip().split()
        if len(parts) != 5:
            return expression

        minute, hour, day, month, weekday = parts

        desc = []
        if weekday != "*":
            days = ["日", "一", "二", "三", "四", "五", "六"]
            desc.append(f"每周{days[int(weekday)]}")
        if hour != "*":
            h = int(hour)
            desc.append(f"每{hour}小时" if hour == "*/1" else f"{h:02d}:{minute.zfill(2)}")
        if day != "*":
            desc.append(f"每月第{day}天")
        if minute == "0" and hour != "*":
            return " ".join(desc) if desc else "每小时"

        return " ".join(desc) if desc else expression

    @staticmethod
    def common_cron_expressions() -> List[Dict[str, str]]:
        """返回常用 Cron 表达式列表。"""
        return [
            {"expr": "0 9 * * *", "label": "每天上午 9 点", "desc": "每日 09:00"},
            {"expr": "0 21 * * *", "label": "每天晚上 9 点", "desc": "每日 21:00"},
            {"expr": "0 */2 * * *", "label": "每 2 小时", "desc": "每 2 小时"},
            {"expr": "0 0 * * 1", "label": "每周一凌晨", "desc": "每周一 00:00"},
            {"expr": "0 0 1 * *", "label": "每月 1 号", "desc": "每月 1 日 00:00"},
            {"expr": "*/30 * * * *", "label": "每 30 分钟", "desc": "每 30 分钟"},
            {"expr": "0 9,21 * * *", "label": "每天早晚 9 点", "desc": "每日 09:00 和 21:00"},
        ]
