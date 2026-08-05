"""任务调度器 — 基于 APScheduler 的定时任务管理。

支持：
- Cron 表达式调度（类 Unix crontab）
- 一次性延迟执行
- 间隔执行
- 任务持久化
- 任务状态管理
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """任务状态。"""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    REMOVED = "removed"


@dataclass
class ScheduledTask:
    """定时任务定义。"""

    id: str
    name: str
    workflow_id: str
    trigger_type: str  # "cron" | "interval" | "date"
    trigger_config: Dict[str, Any]
    status: TaskStatus = TaskStatus.ACTIVE
    created_at: float = 0.0
    last_run: Optional[float] = None
    next_run: Optional[float] = None
    run_count: int = 0
    description: str = ""
    tags: List[str] = field(default_factory=list)


class TaskScheduler:
    """任务调度器 — 管理和执行定时自动化任务。

    基于 APScheduler 实现，提供：
    - Cron 定时任务
    - 间隔执行任务
    - 一次性任务
    - 任务持久化（JSON 文件）
    - 启用/禁用任务
    """

    def __init__(self, task_store_path: str = ""):
        self._scheduler = AsyncIOScheduler()
        self._tasks: Dict[str, ScheduledTask] = {}
        self._job_map: Dict[str, str] = {}  # task_id -> job_id
        self._executors: Dict[str, Callable] = {}
        self._task_store_path = task_store_path

    def start(self) -> None:
        """启动调度器。"""
        self._scheduler.start()
        logger.info("任务调度器已启动")

    def shutdown(self, wait: bool = True) -> None:
        """关闭调度器。"""
        self._scheduler.shutdown(wait=wait)
        logger.info("任务调度器已关闭")

    def register_executor(self, task_id: str, executor: Callable) -> None:
        """注册任务执行器。"""
        self._executors[task_id] = executor

    def add_cron_task(
        self,
        name: str,
        workflow_id: str,
        cron_expression: str,
        executor: Callable,
        description: str = "",
        tags: List[str] = None,
    ) -> str:
        """添加 Cron 定时任务。

        Args:
            name: 任务名称
            workflow_id: 关联的工作流 ID
            cron_expression: Cron 表达式，如 "0 21 * * *"（每天 21:00）
            executor: 执行回调函数
            description: 任务描述
            tags: 标签列表

        Returns:
            str: 任务 ID
        """
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = ScheduledTask(
            id=task_id,
            name=name,
            workflow_id=workflow_id,
            trigger_type="cron",
            trigger_config={"cron": cron_expression},
            description=description,
            tags=tags or [],
            created_at=time.time(),
        )
        self._tasks[task_id] = task
        self._executors[task_id] = executor

        # 注册到 APScheduler
        trigger = CronTrigger.from_crontab(cron_expression)
        job = self._scheduler.add_job(
            self._run_task,
            trigger,
            args=[task_id],
            id=f"job_{task_id}",
            name=name,
            misfire_grace_time=300,
        )
        self._job_map[task_id] = job.id

        # 计算下次运行时间
        if job.next_run_time:
            task.next_run = job.next_run_time.timestamp()

        logger.info(f"已添加定时任务 '{name}' ({cron_expression}): {task_id}")
        return task_id

    def add_interval_task(
        self,
        name: str,
        workflow_id: str,
        minutes: int = 0,
        hours: int = 0,
        days: int = 0,
        executor: Callable = None,
        description: str = "",
    ) -> str:
        """添加间隔任务。"""
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = ScheduledTask(
            id=task_id,
            name=name,
            workflow_id=workflow_id,
            trigger_type="interval",
            trigger_config={"minutes": minutes, "hours": hours, "days": days},
            description=description,
            created_at=time.time(),
        )
        self._tasks[task_id] = task
        if executor:
            self._executors[task_id] = executor

        trigger = IntervalTrigger(minutes=minutes, hours=hours, days=days)
        job = self._scheduler.add_job(
            self._run_task,
            trigger,
            args=[task_id],
            id=f"job_{task_id}",
            name=name,
        )
        self._job_map[task_id] = job.id

        if job.next_run_time:
            task.next_run = job.next_run_time.timestamp()

        logger.info(f"已添加间隔任务 '{name}' (每{minutes}分钟): {task_id}")
        return task_id

    async def _run_task(self, task_id: str) -> None:
        """运行任务（内部回调）。"""
        task = self._tasks.get(task_id)
        if not task:
            logger.error(f"任务 {task_id} 不存在")
            return

        if task.status != TaskStatus.ACTIVE:
            logger.warning(f"任务 {task.name} 状态不是 active，跳过")
            return

        executor = self._executors.get(task_id)
        if not executor:
            logger.error(f"任务 {task_id} 没有注册执行器")
            return

        logger.info(f"开始执行定时任务: {task.name}")
        task.run_count += 1
        task.last_run = time.time()

        try:
            if asyncio.iscoroutinefunction(executor):
                await executor()
            else:
                executor()
            logger.info(f"定时任务执行完成: {task.name}")
        except Exception as e:
            task.status = TaskStatus.FAILED
            logger.error(f"定时任务执行失败 '{task.name}': {e}")

        # 更新下次运行时间
        job = self._scheduler.get_job(f"job_{task_id}")
        if job and job.next_run_time:
            task.next_run = job.next_run_time.timestamp()

    def pause_task(self, task_id: str) -> bool:
        """暂停任务。"""
        job_id = self._job_map.get(task_id)
        if not job_id:
            return False
        self._scheduler.pause_job(job_id)
        if task_id in self._tasks:
            self._tasks[task_id].status = TaskStatus.PAUSED
        return True

    def resume_task(self, task_id: str) -> bool:
        """恢复任务。"""
        job_id = self._job_map.get(task_id)
        if not job_id:
            return False
        self._scheduler.resume_job(job_id)
        if task_id in self._tasks:
            self._tasks[task_id].status = TaskStatus.ACTIVE
        return True

    def remove_task(self, task_id: str) -> bool:
        """删除任务。"""
        job_id = self._job_map.get(task_id)
        if job_id:
            self._scheduler.remove_job(job_id)
        self._job_map.pop(task_id, None)
        task = self._tasks.pop(task_id, None)
        if task:
            task.status = TaskStatus.REMOVED
        return task is not None

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        """获取任务信息。"""
        return self._tasks.get(task_id)

    def list_tasks(self, status: Optional[TaskStatus] = None) -> List[ScheduledTask]:
        """列出所有任务。"""
        if status:
            return [t for t in self._tasks.values() if t.status == status]
        return list(self._tasks.values())

    def list_active_tasks(self) -> List[ScheduledTask]:
        """列出所有活跃任务。"""
        return self.list_tasks(TaskStatus.ACTIVE)
