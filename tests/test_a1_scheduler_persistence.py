"""A-1 scheduler task persistence tests — save→load round-trip, restart keeps cron tasks."""

from __future__ import annotations

import json
from pathlib import Path

from fusion_cowork.engine.scheduler import TaskScheduler, TaskStatus


def _make_scheduler(store: Path) -> TaskScheduler:
    return TaskScheduler(task_store_path=str(store))


def test_add_cron_task_persists_to_disk(tmp_path):
    store = tmp_path / "tasks.json"
    sched = _make_scheduler(store)
    tid = sched.add_cron_task(
        name="夜跑清理",  # i18n: user-facing
        workflow_id="wf_1",
        cron_expression="0 21 * * *",
        executor=lambda: None,
    )
    assert store.exists(), "add_cron_task 后应立即落地 JSON"  # i18n: user-facing
    data = json.loads(store.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["id"] == tid
    assert data[0]["trigger_type"] == "cron"
    assert data[0]["trigger_config"]["cron"] == "0 21 * * *"
    assert data[0]["status"] == "active"


def test_persist_atomic_no_half_file(tmp_path):
    # E-4: persisted via tmp+os.replace, a mid-write crash leaves no half-written .tmp
    store = tmp_path / "tasks.json"
    sched = _make_scheduler(store)
    sched.add_cron_task(name="t", workflow_id="wf", cron_expression="0 0 * * *", executor=lambda: None)
    assert not (tmp_path / "tasks.json.tmp").exists(), "原子写后应无残留 .tmp"  # i18n: user-facing


def test_load_restores_tasks_after_restart(tmp_path):
    store = tmp_path / "tasks.json"
    # Simulate first app run: create tasks + persist
    sched1 = _make_scheduler(store)
    tid = sched1.add_cron_task(
        name="每日备份",  # i18n: user-facing
        workflow_id="wf_bak",
        cron_expression="30 3 * * *",
        executor=lambda: None,
    )
    sched1.add_interval_task(
        name="心跳",  # i18n: user-facing
        workflow_id="wf_hb",
        minutes=5,
        executor=lambda: None,
    )
    assert len(json.loads(store.read_text(encoding="utf-8"))) == 2

    # Simulate restart: new instance (executors lost, pending re-registration)
    sched2 = _make_scheduler(store)
    restored = sched2.load_tasks()
    assert restored == 2, f"应恢复 2 个任务, 实际 {restored}"  # i18n: user-facing
    t = sched2.get_task(tid)
    assert t is not None
    assert t.name == "每日备份"  # i18n: user-facing
    assert t.trigger_type == "cron"
    assert t.trigger_config["cron"] == "30 3 * * *"
    # P1-12 (audit 0912): restored ACTIVE task without an executor is
    # auto-paused instead of staying ACTIVE and firing into the void.
    assert t.status == TaskStatus.PAUSED
    # re-registering the executor resumes it
    sched2.register_executor(tid, lambda: None)
    assert sched2.get_task(tid).status == TaskStatus.ACTIVE


def test_load_warns_missing_executor_but_task_restored(tmp_path, caplog):
    import logging

    store = tmp_path / "tasks.json"
    sched1 = _make_scheduler(store)
    tid = sched1.add_cron_task(
        name="无执行器",  # i18n: user-facing
        workflow_id="wf",
        cron_expression="0 0 * * *",
        executor=lambda: None,  # i18n: user-facing
    )  # i18n: user-facing
    sched2 = _make_scheduler(store)
    with caplog.at_level(logging.WARNING):
        sched2.load_tasks()
    assert sched2.get_task(tid) is not None, "无 executor 也应恢复任务定义"  # i18n: user-facing
    assert any("has no executor yet" in r.message for r in caplog.records), "should log WARN to prompt re-registration"


def test_register_executor_makes_task_runnable(tmp_path, caplog):
    import logging

    store = tmp_path / "tasks.json"
    sched1 = _make_scheduler(store)
    tid = sched1.add_cron_task(
        name="重注册",  # i18n: user-facing
        workflow_id="wf",
        cron_expression="0 0 * * *",
        executor=lambda: None,  # i18n: user-facing
    )  # i18n: user-facing
    sched2 = _make_scheduler(store)
    sched2.load_tasks()
    with caplog.at_level(logging.INFO):
        sched2.register_executor(tid, lambda: None)
    assert any("executor registered" in r.message for r in caplog.records)


def test_pause_resume_persists_status(tmp_path):
    store = tmp_path / "tasks.json"
    sched = _make_scheduler(store)
    tid = sched.add_cron_task(
        name="可暂停",  # i18n: user-facing
        workflow_id="wf",
        cron_expression="0 0 * * *",
        executor=lambda: None,  # i18n: user-facing
    )  # i18n: user-facing
    assert sched.pause_task(tid)
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data[0]["status"] == "paused", "pause 应落地 paused"  # i18n: user-facing
    assert sched.resume_task(tid)
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data[0]["status"] == "active", "resume 应落地 active"  # i18n: user-facing
    assert data[0]["fail_count"] == 0, "resume 应重置 fail_count"  # i18n: user-facing


def test_remove_task_persists_removal(tmp_path):
    store = tmp_path / "tasks.json"
    sched = _make_scheduler(store)
    tid = sched.add_cron_task(
        name="待删",  # i18n: user-facing
        workflow_id="wf",
        cron_expression="0 0 * * *",
        executor=lambda: None,  # i18n: user-facing
    )  # i18n: user-facing
    assert sched.remove_task(tid)
    data = json.loads(store.read_text(encoding="utf-8"))
    assert len(data) == 0, "remove 应从持久化移除"  # i18n: user-facing


def test_load_ignores_completed_failed_keeps_record(tmp_path):
    # COMPLETED/FAILED do not rebuild the job, but the definition is kept for querying
    store = tmp_path / "tasks.json"
    store.write_text(
        json.dumps(
            [
                {
                    "id": "task_done",
                    "name": "已完成",  # i18n: user-facing
                    "workflow_id": "wf",
                    "trigger_type": "cron",
                    "trigger_config": {"cron": "0 0 * * *"},
                    "status": "completed",
                    "created_at": 0.0,
                    "last_run": None,
                    "next_run": None,
                    "run_count": 1,
                    "fail_count": 0,
                    "description": "",
                    "tags": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    sched = _make_scheduler(store)
    restored = sched.load_tasks()
    assert restored == 1, "已完成任务也应恢复定义"  # i18n: user-facing
    assert sched.get_task("task_done").status == TaskStatus.COMPLETED
    assert "task_done" not in sched._job_map, "COMPLETED 不重建 APScheduler job"  # i18n: user-facing


def test_load_corrupt_json_returns_zero(tmp_path, caplog):
    import logging

    store = tmp_path / "tasks.json"
    store.write_text("{not json", encoding="utf-8")
    sched = _make_scheduler(store)
    with caplog.at_level(logging.WARNING):
        assert sched.load_tasks() == 0
    assert any("Failed to load scheduled tasks" in r.message for r in caplog.records)


def test_default_store_path_when_empty():
    # Empty path → falls back to _DEFAULT_STORE_PATH (~/.fusion-cowork/...)
    sched = TaskScheduler(task_store_path="")
    from fusion_cowork.engine.scheduler import _DEFAULT_STORE_PATH

    assert sched._task_store_path == _DEFAULT_STORE_PATH
