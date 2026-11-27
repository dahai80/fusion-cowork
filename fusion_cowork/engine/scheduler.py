"""Task scheduler — APScheduler-based scheduled task management.

Supports:
- cron expression scheduling (Unix crontab style)
- one-shot delayed execution
- interval execution
- task persistence
- task status management
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from fusion_cowork.tenant import DEFAULT_TENANT, resolve_tenant_id

logger = logging.getLogger(__name__)

_DEFAULT_STORE_PATH = str(Path.home() / ".fusion-cowork" / "scheduled_tasks.json")


class TaskStatus(Enum):
    """Task status."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    REMOVED = "removed"


@dataclass
class ScheduledTask:
    """Scheduled task definition."""

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
    fail_count: int = 0
    description: str = ""
    tags: List[str] = field(default_factory=list)
    tenant_id: str = DEFAULT_TENANT


class TaskScheduler:
    """Task scheduler — manages and executes scheduled automation tasks.

    Built on APScheduler, provides:
    - cron scheduled tasks
    - interval tasks
    - one-shot tasks
    - task persistence (JSON file)
    - enable/disable tasks
    """

    def __init__(self, task_store_path: str = ""):
        self._scheduler = AsyncIOScheduler()
        self._tasks: Dict[str, ScheduledTask] = {}
        self._job_map: Dict[str, str] = {}  # task_id -> job_id
        self._executors: Dict[str, Callable] = {}
        # A-1: task_store_path wiring — empty uses the default path. persist_tasks/load_tasks persist task definitions,
        # restart no longer loses all scheduled tasks (legacy stored the _task_store_path field but never saved/loaded — dead config).
        self._task_store_path = task_store_path or _DEFAULT_STORE_PATH
        # R-5: consecutive-failure cap, only beyond it FAILED+pause (avoids a single transient failure stopping the task forever).
        self._max_fail = 5
        # P1-12 (audit 0912): task_ids restored without an executor — paused
        # until register_executor() re-binds and resumes them.
        self._executors_paused: Dict[str, bool] = {}

    def start(self) -> None:
        """Start the scheduler."""
        self._scheduler.start()
        # A-1: restore task definitions from disk at startup (the executor must re-register them — callables are not serializable).
        restored = self.load_tasks()
        if restored:
            logger.info(f"Scheduler startup restored {restored} persisted tasks (executor pending app re-registration)")
        logger.info("Task scheduler started")

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the scheduler."""
        self._scheduler.shutdown(wait=wait)
        logger.info("Task scheduler shut down")

    def register_executor(self, task_id: str, executor: Callable) -> None:
        """Register a task executor."""
        self._executors[task_id] = executor
        # A-1: restored tasks previously had no executor (load_tasks logs a WARN); after re-registration log INFO that they are runnable.
        task = self._tasks.get(task_id)
        # P1-12 (audit 0912): re-binding an executor resumes a task that was
        # auto-paused at restore time (marker True). User-paused tasks have no
        # marker and stay paused.
        if task and self._executors_paused.pop(task_id, False) and task.status == TaskStatus.PAUSED:
            task.status = TaskStatus.ACTIVE
            # auto-paused tasks skipped job rebuild at restore time — rebuild now
            self._rebuild_job(task)
            logger.info(f"Task '{task.name}' ({task_id}) executor registered, auto-resuming execution")
        elif task:
            logger.info(f"Task '{task.name}' ({task_id}) executor registered, runnable")

    def add_cron_task(
        self,
        name: str,
        workflow_id: str,
        cron_expression: str,
        executor: Callable,
        description: str = "",
        tags: List[str] = None,
        tenant_id: Optional[str] = None,
    ) -> str:
        """Add a cron scheduled task.

        Args:
            name: task name
            workflow_id: associated workflow ID
            cron_expression: cron expression, e.g. "0 21 * * *" (daily 21:00)
            executor: execution callback
            description: task description
            tags: tag list
            tenant_id: tenant ID (v0.4.0 multi-tenant isolation, falls back to the contextvar when absent)

        Returns:
            str: task ID
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
            tenant_id=resolve_tenant_id(tenant_id),
        )
        self._tasks[task_id] = task
        self._executors[task_id] = executor

        # Register with APScheduler
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

        # Compute next run time (job has no next_run_time before the scheduler starts — getattr fallback)
        nrt = getattr(job, "next_run_time", None)
        if nrt:
            task.next_run = nrt.timestamp()

        logger.info(f"Added cron task '{name}' ({cron_expression}): {task_id}")
        # A-1: persist the task definition (not lost on restart)
        self._persist_tasks()
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
        tenant_id: Optional[str] = None,
    ) -> str:
        """Add an interval task."""
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = ScheduledTask(
            id=task_id,
            name=name,
            workflow_id=workflow_id,
            trigger_type="interval",
            trigger_config={"minutes": minutes, "hours": hours, "days": days},
            description=description,
            created_at=time.time(),
            tenant_id=resolve_tenant_id(tenant_id),
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

        nrt = getattr(job, "next_run_time", None)
        if nrt:
            task.next_run = nrt.timestamp()

        logger.info(f"Added interval task '{name}' (every {minutes} minutes): {task_id}")
        # A-1: persist the task definition (not lost on restart)
        self._persist_tasks()
        return task_id

    async def _run_task(self, task_id: str) -> None:
        """Run a task (internal callback)."""
        task = self._tasks.get(task_id)
        if not task:
            logger.error(f"Task {task_id} does not exist")
            return

        if task.status != TaskStatus.ACTIVE:
            logger.warning(f"Task {task.name} status is not active, skipping")
            return

        executor = self._executors.get(task_id)
        if not executor:
            logger.error(f"Task {task_id} has no registered executor")
            return

        logger.info(f"Starting scheduled task: {task.name}")
        task.run_count += 1
        task.last_run = time.time()

        try:
            if asyncio.iscoroutinefunction(executor):
                await executor()
            else:
                executor()
            logger.info(f"Scheduled task completed: {task.name}")
            task.fail_count = 0
        except Exception as e:
            # R-5: legacy marked FAILED directly → the APScheduler job stayed active and silently skipped on the next fire because status!=ACTIVE,
            # the user never knew the task had stalled (audit: permanent stall). Now: count failures, restore ACTIVE for a retry next fire under the threshold;
            # only beyond the threshold FAILED + pause the job (explicit stop, never a silent skip).
            task.fail_count = getattr(task, "fail_count", 0) + 1
            logger.error(f"Scheduled task failed '{task.name}' (attempt {task.fail_count}): {e}")
            if task.fail_count >= self._max_fail:
                task.status = TaskStatus.FAILED
                logger.warning(
                    f"Scheduled task '{task.name}' failed {task.fail_count} times in a row, marked FAILED and paused (resume manually via resume_task)"
                )
                job_id = self._job_map.get(task_id)
                if job_id:
                    try:
                        self._scheduler.pause_job(job_id)
                    except Exception as pe:
                        logger.error(f"Failed to pause job after FAILED '{task.name}': {pe}")
            else:
                logger.info(f"Scheduled task '{task.name}' transient failure, stays ACTIVE and retries on next fire")
            # A-1: persist the status/count change
            self._persist_tasks()

        # Update next run time
        job = self._scheduler.get_job(f"job_{task_id}")
        if job:
            nrt = getattr(job, "next_run_time", None)
            if nrt:
                task.next_run = nrt.timestamp()

    def pause_task(self, task_id: str) -> bool:
        """Pause a task."""
        job_id = self._job_map.get(task_id)
        if not job_id:
            return False
        # R-5: the job may already be paused (on FAILED) or removed → tolerate JobLookupError.
        try:
            self._scheduler.pause_job(job_id)
        except Exception as e:
            logger.warning(f"pause_job '{task_id}' failed (job may already be removed): {e}")
        if task_id in self._tasks:
            self._tasks[task_id].status = TaskStatus.PAUSED
        # A-1: persist the status change
        self._persist_tasks()
        return True

    def resume_task(self, task_id: str) -> bool:
        """Resume a task."""
        job_id = self._job_map.get(task_id)
        if not job_id:
            return False
        try:
            self._scheduler.resume_job(job_id)
        except Exception as e:
            logger.warning(f"resume_job '{task_id}' failed (job may already be removed): {e}")
            return False
        if task_id in self._tasks:
            self._tasks[task_id].status = TaskStatus.ACTIVE
            # Reset the failure count on resume, allowing retries.
            self._tasks[task_id].fail_count = 0
        # A-1: persist the status change
        self._persist_tasks()
        return True

    def remove_task(self, task_id: str) -> bool:
        """Delete a task."""
        job_id = self._job_map.get(task_id)
        if job_id:
            try:
                self._scheduler.remove_job(job_id)
            except Exception as e:
                logger.warning(f"remove_job '{task_id}' failed (job may already be removed): {e}")
        self._job_map.pop(task_id, None)
        task = self._tasks.pop(task_id, None)
        if task:
            task.status = TaskStatus.REMOVED
        # A-1: persist the removal
        self._persist_tasks()
        return task is not None

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        """Get task info."""
        return self._tasks.get(task_id)

    async def run_task_now(self, task_id: str) -> bool:
        # issue #608: operator run-now — execute the task's workflow immediately
        # with the same bookkeeping as a cron fire (_run_task bumps run_count/
        # last_run and applies the fail threshold). Paused/failed tasks refuse
        # explicitly so the GUI can surface 409 instead of a silent skip.
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status != TaskStatus.ACTIVE:
            raise ValueError(f"task not active: {task.status.value}")
        await self._run_task(task_id)
        return True

    def list_tasks(
        self,
        status: Optional[TaskStatus] = None,
        tenant_id: Optional[str] = None,
    ) -> List[ScheduledTask]:
        """List all tasks (v0.4.0: filtered by tenant_id to prevent cross-tenant leakage)."""
        tid = resolve_tenant_id(tenant_id)
        tasks = [t for t in self._tasks.values() if t.tenant_id == tid]
        if status:
            return [t for t in tasks if t.status == status]
        return tasks

    def list_active_tasks(self) -> List[ScheduledTask]:
        """List all active tasks."""
        return self.list_tasks(TaskStatus.ACTIVE)

    # ── A-1 task persistence ──

    def _task_to_dict(self, task: ScheduledTask) -> Dict[str, Any]:
        return {
            "id": task.id,
            "name": task.name,
            "workflow_id": task.workflow_id,
            "trigger_type": task.trigger_type,
            "trigger_config": task.trigger_config,
            "status": task.status.value,
            "created_at": task.created_at,
            "last_run": task.last_run,
            "next_run": task.next_run,
            "run_count": task.run_count,
            "fail_count": task.fail_count,
            "description": task.description,
            "tags": list(task.tags),
            "tenant_id": task.tenant_id,
        }

    def _persist_tasks(self) -> None:
        # A-1: persist task definitions (the executor is not serialized — a callable, re-registered after app restart).
        # E-4 atomic write: tmp+os.replace, avoids a half-written JSON on crash breaking load parsing.
        path = Path(self._task_store_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                [self._task_to_dict(t) for t in self._tasks.values()],
                ensure_ascii=False,
                indent=2,
            )
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            if "tmp" in dir() and tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            logger.exception(f"Failed to persist scheduled tasks: {self._task_store_path}")
            raise

    def load_tasks(self) -> int:
        # A-1: restore task definitions at startup + rebuild APScheduler jobs. Missing executor logs a WARN (runnable after app re-registration).
        # Only restore ACTIVE/PAUSED (REMOVED/COMPLETED/FAILED do not rebuild jobs; records kept for querying).
        path = Path(self._task_store_path)
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load scheduled tasks (ignored, starting empty): {e}")
            return 0
        restored = 0
        for item in data:
            try:
                task_id = item.get("id", "")
                if not task_id or task_id in self._tasks:
                    continue
                status_str = item.get("status", "active")
                try:
                    status = TaskStatus(status_str)
                except ValueError:
                    status = TaskStatus.ACTIVE
                task = ScheduledTask(
                    id=task_id,
                    name=item.get("name", ""),
                    workflow_id=item.get("workflow_id", ""),
                    trigger_type=item.get("trigger_type", "cron"),
                    trigger_config=item.get("trigger_config", {}),
                    status=status,
                    created_at=float(item.get("created_at", 0.0) or 0.0),
                    last_run=item.get("last_run"),
                    next_run=item.get("next_run"),
                    run_count=int(item.get("run_count", 0) or 0),
                    fail_count=int(item.get("fail_count", 0) or 0),
                    description=item.get("description", ""),
                    tags=list(item.get("tags", [])),
                    tenant_id=item.get("tenant_id", DEFAULT_TENANT),
                )
                self._tasks[task_id] = task
                # P1-12 (audit 0912): a restored task without a registered
                # executor can never run — previously it stayed ACTIVE, its
                # APScheduler job fired and silently returned ("no registered executor"),
                # so the business automation died silently after a restart.
                # Now it is paused until the app re-registers the executor,
                # and register_executor() resumes it.
                if task_id not in self._executors:
                    # P1-12: only ORIGINALLY-ACTIVE tasks are auto-paused with a
                    # marker; user-paused tasks (status == PAUSED on disk) keep
                    # their pause without a marker and are never auto-resumed.
                    if status == TaskStatus.ACTIVE:
                        logger.warning(
                            f"Restored task '{task.name}' ({task_id}) has no executor yet, paused pending re-registration"
                        )
                        task.status = TaskStatus.PAUSED
                        self._executors_paused[task_id] = True
                    restored += 1
                    continue
                # Only ACTIVE/PAUSED rebuild APScheduler jobs (COMPLETED/FAILED/REMOVED do not).
                if status in (TaskStatus.ACTIVE, TaskStatus.PAUSED):
                    self._rebuild_job(task)
                    if status == TaskStatus.PAUSED:
                        job_id = self._job_map.get(task_id)
                        if job_id:
                            try:
                                self._scheduler.pause_job(job_id)
                            except Exception:
                                pass
                restored += 1
            except Exception as e:
                logger.warning(f"Failed to restore scheduled task item (skipped): {e}")
        return restored

    def _rebuild_job(self, task: ScheduledTask) -> None:
        # A-1: rebuild the APScheduler job by trigger_type (used by load_tasks).
        try:
            if task.trigger_type == "cron":
                trigger = CronTrigger.from_crontab(task.trigger_config.get("cron", "* * * * *"))
            elif task.trigger_type == "interval":
                trigger = IntervalTrigger(
                    minutes=int(task.trigger_config.get("minutes", 0) or 0),
                    hours=int(task.trigger_config.get("hours", 0) or 0),
                    days=int(task.trigger_config.get("days", 0) or 0),
                )
            else:
                logger.warning(f"Restored task '{task.name}' trigger_type={task.trigger_type} unsupported, skipping")
                return
            job = self._scheduler.add_job(
                self._run_task,
                trigger,
                args=[task.id],
                id=f"job_{task.id}",
                name=task.name,
                misfire_grace_time=300,
            )
            self._job_map[task.id] = job.id
            nrt = getattr(job, "next_run_time", None)
            if nrt:
                task.next_run = nrt.timestamp()
        except Exception as e:
            logger.warning(f"Failed to rebuild task job '{task.name}': {e}")
