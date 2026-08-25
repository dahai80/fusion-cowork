"""A-1 调度任务持久化测试 — save→load 往返, restart 不丢定时任务。"""

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
        name="夜跑清理",
        workflow_id="wf_1",
        cron_expression="0 21 * * *",
        executor=lambda: None,
    )
    assert store.exists(), "add_cron_task 后应立即落地 JSON"
    data = json.loads(store.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["id"] == tid
    assert data[0]["trigger_type"] == "cron"
    assert data[0]["trigger_config"]["cron"] == "0 21 * * *"
    assert data[0]["status"] == "active"


def test_persist_atomic_no_half_file(tmp_path):
    # E-4: 落地用 tmp+os.replace, 中途崩溃不留半截 .tmp
    store = tmp_path / "tasks.json"
    sched = _make_scheduler(store)
    sched.add_cron_task(name="t", workflow_id="wf", cron_expression="0 0 * * *", executor=lambda: None)
    assert not (tmp_path / "tasks.json.tmp").exists(), "原子写后应无残留 .tmp"


def test_load_restores_tasks_after_restart(tmp_path):
    store = tmp_path / "tasks.json"
    # 模拟应用第一次: 建任务 + 落地
    sched1 = _make_scheduler(store)
    tid = sched1.add_cron_task(
        name="每日备份",
        workflow_id="wf_bak",
        cron_expression="30 3 * * *",
        executor=lambda: None,
    )
    sched1.add_interval_task(
        name="心跳",
        workflow_id="wf_hb",
        minutes=5,
        executor=lambda: None,
    )
    assert len(json.loads(store.read_text(encoding="utf-8"))) == 2

    # 模拟 restart: 新实例 (executor 丢失, 待重注册)
    sched2 = _make_scheduler(store)
    restored = sched2.load_tasks()
    assert restored == 2, f"应恢复 2 个任务, 实际 {restored}"
    t = sched2.get_task(tid)
    assert t is not None
    assert t.name == "每日备份"
    assert t.trigger_type == "cron"
    assert t.trigger_config["cron"] == "30 3 * * *"
    assert t.status == TaskStatus.ACTIVE


def test_load_warns_missing_executor_but_task_restored(tmp_path, caplog):
    import logging

    store = tmp_path / "tasks.json"
    sched1 = _make_scheduler(store)
    tid = sched1.add_cron_task(name="无执行器", workflow_id="wf", cron_expression="0 0 * * *", executor=lambda: None)
    sched2 = _make_scheduler(store)
    with caplog.at_level(logging.WARNING):
        sched2.load_tasks()
    assert sched2.get_task(tid) is not None, "无 executor 也应恢复任务定义"
    assert any("暂无 executor" in r.message for r in caplog.records), "应记 WARN 提示重注册"


def test_register_executor_makes_task_runnable(tmp_path, caplog):
    import logging

    store = tmp_path / "tasks.json"
    sched1 = _make_scheduler(store)
    tid = sched1.add_cron_task(name="重注册", workflow_id="wf", cron_expression="0 0 * * *", executor=lambda: None)
    sched2 = _make_scheduler(store)
    sched2.load_tasks()
    with caplog.at_level(logging.INFO):
        sched2.register_executor(tid, lambda: None)
    assert any("executor 已注册" in r.message for r in caplog.records)


def test_pause_resume_persists_status(tmp_path):
    store = tmp_path / "tasks.json"
    sched = _make_scheduler(store)
    tid = sched.add_cron_task(name="可暂停", workflow_id="wf", cron_expression="0 0 * * *", executor=lambda: None)
    assert sched.pause_task(tid)
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data[0]["status"] == "paused", "pause 应落地 paused"
    assert sched.resume_task(tid)
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data[0]["status"] == "active", "resume 应落地 active"
    assert data[0]["fail_count"] == 0, "resume 应重置 fail_count"


def test_remove_task_persists_removal(tmp_path):
    store = tmp_path / "tasks.json"
    sched = _make_scheduler(store)
    tid = sched.add_cron_task(name="待删", workflow_id="wf", cron_expression="0 0 * * *", executor=lambda: None)
    assert sched.remove_task(tid)
    data = json.loads(store.read_text(encoding="utf-8"))
    assert len(data) == 0, "remove 应从持久化移除"


def test_load_ignores_completed_failed_keeps_record(tmp_path):
    # COMPLETED/FAILED 不重建 job, 但定义留记录供查询
    store = tmp_path / "tasks.json"
    store.write_text(
        json.dumps(
            [
                {
                    "id": "task_done",
                    "name": "已完成",
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
    assert restored == 1, "已完成任务也应恢复定义"
    assert sched.get_task("task_done").status == TaskStatus.COMPLETED
    assert "task_done" not in sched._job_map, "COMPLETED 不重建 APScheduler job"


def test_load_corrupt_json_returns_zero(tmp_path, caplog):
    import logging

    store = tmp_path / "tasks.json"
    store.write_text("{not json", encoding="utf-8")
    sched = _make_scheduler(store)
    with caplog.at_level(logging.WARNING):
        assert sched.load_tasks() == 0
    assert any("加载调度任务失败" in r.message for r in caplog.records)


def test_default_store_path_when_empty():
    # 空路径 → 用 _DEFAULT_STORE_PATH (~/.fusion-cowork/...)
    sched = TaskScheduler(task_store_path="")
    from fusion_cowork.engine.scheduler import _DEFAULT_STORE_PATH

    assert sched._task_store_path == _DEFAULT_STORE_PATH
