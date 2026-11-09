"""issue #399: RunRegistry.list() 只读内存 — 重启后运行历史从 /api/runs 消失 (F3)。

- run_summaries schema v4: +space_id 列 (v3 库幂等迁移)
- list() 兜底合并持久化终态摘要 (内存优先, 上限 100, space_id 过滤生效)
- 形状对齐内存行 (review=None + summary_row 标记)
"""

import sqlite3
import time

from fusion_cowork.engine.run_persistence import SCHEMA_VERSION, RunStateStore
from fusion_cowork.server.web_api import RunRegistry


class _NoEngine:
    def hitl_pending(self, run_id):
        return False


def _summary_record(run_id: str, status: str = "success", **kw) -> dict:
    base = {
        "run_id": run_id,
        "workflow_id": "wf_1",
        "workflow_name": "t399",
        "status": status,
        "source": "pipeline",
        "labels": {"task": "9"},
        "space_id": "",
        "created_at": time.time() - 100,
        "started_at": time.time() - 90,
        "finished_at": time.time(),
        "error": "boom" if status == "failed" else None,
        "steps": [{"node_id": "n1", "node_name": "Fetch", "status": "success", "execution_time": 1.0, "attempt": 1}],
    }
    base.update(kw)
    return base


class TestSchemaV4:
    def test_fresh_db_summary_has_space_id(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(run_summaries)").fetchall()}
        assert "space_id" in cols
        assert SCHEMA_VERSION == 4
        store.close()

    def test_v3_db_migrates_idempotent(self, tmp_path):
        # 手造 v3 库 (run_summaries 无 space_id) → 打开即迁移, 数据保留
        p = tmp_path / "v3.db"
        conn = sqlite3.connect(str(p))
        conn.executescript(
            """
            CREATE TABLE run_states (
                run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL DEFAULT '',
                workflow_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'running',
                paused_reason TEXT NOT NULL DEFAULT '', workflow_def TEXT NOT NULL DEFAULT '{}',
                steps TEXT NOT NULL DEFAULT '[]', directives TEXT NOT NULL DEFAULT '[]',
                memory INTEGER NOT NULL DEFAULT 1, space_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '', labels TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE run_summaries (
                run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL DEFAULT '',
                workflow_name TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                labels TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT '',
                error TEXT, steps TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL DEFAULT 0, started_at REAL NOT NULL DEFAULT 0,
                finished_at REAL NOT NULL DEFAULT 0
            );
            PRAGMA user_version = 3;
            """
        )
        conn.execute("INSERT INTO run_summaries (run_id, status) VALUES ('run_old', 'success')")
        conn.commit()
        conn.close()
        store = RunStateStore(p)
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(run_summaries)").fetchall()}
        assert "space_id" in cols
        assert store.load_summary("run_old")["run_id"] == "run_old"
        store.close()


class TestListFallback:
    def _registry(self, store) -> RunRegistry:
        return RunRegistry(_NoEngine(), run_store=store)

    def test_list_returns_persisted_run_after_restart(self, tmp_path):
        path = tmp_path / "rs.db"
        store = RunStateStore(path)
        store.save_summary(_summary_record("run_hist1"))
        store.close()
        # "重启": 全新 store + 空 registry, 只共享同一 db 文件
        registry = self._registry(RunStateStore(path))
        items = registry.list()
        assert [r["run_id"] for r in items] == ["run_hist1"]
        row = items[0]
        assert row["workflow_name"] == "t399"
        assert row["status"] == "success"
        assert row["source"] == "pipeline"
        assert row["labels"] == {"task": "9"}
        assert row["review"] is None
        assert row["summary_row"] is True
        assert row["space_id"] == ""
        assert row["error"] is None

    def test_memory_wins_on_conflict(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        store.save_summary(_summary_record("run_live", status="success"))
        registry = self._registry(store)
        registry._runs["run_live"] = {
            "run_id": "run_live",
            "workflow_id": "wf_1",
            "workflow_name": "live-version",
            "status": "running",
            "created_at": time.time(),
            "space_id": "",
        }
        items = registry.list()
        assert len(items) == 1
        assert items[0]["status"] == "running"
        assert items[0]["workflow_name"] == "live-version"
        assert "summary_row" not in items[0]

    def test_space_id_filter_honored(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        store.save_summary(_summary_record("run_a", space_id="sp_1"))
        store.save_summary(_summary_record("run_b", space_id=""))
        registry = self._registry(store)
        ids = [r["run_id"] for r in registry.list(space_id="sp_1")]
        assert ids == ["run_a"]
        assert {r["run_id"] for r in registry.list()} == {"run_a", "run_b"}

    def test_newest_first_merged_order(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        now = time.time()
        store.save_summary(_summary_record("run_old", created_at=now - 500, finished_at=now - 400))
        store.save_summary(_summary_record("run_new", created_at=now - 10, finished_at=now - 5))
        registry = self._registry(store)
        registry._runs["run_mid"] = {
            "run_id": "run_mid",
            "workflow_id": "wf_1",
            "workflow_name": "m",
            "status": "running",
            "created_at": now - 100,
            "space_id": "",
        }
        ids = [r["run_id"] for r in registry.list()]
        assert ids == ["run_new", "run_mid", "run_old"]

    def test_persisted_cap_100(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        for i in range(130):
            store.save_summary(_summary_record(f"run_{i:03d}", created_at=time.time() - i, finished_at=time.time() - i))
        registry = self._registry(store)
        items = registry.list()
        assert len(items) == 100
        # newest 100 kept
        assert "run_000" in {r["run_id"] for r in items}
        assert "run_129" not in {r["run_id"] for r in items}

    def test_store_error_degrades_to_memory_only(self, tmp_path):
        class _BrokenStore:
            def load_summaries(self, limit=100):
                raise RuntimeError("db locked")

        registry = self._registry(_BrokenStore())
        registry._runs["run_live"] = {
            "run_id": "run_live",
            "workflow_id": "wf_1",
            "workflow_name": "m",
            "status": "running",
            "created_at": time.time(),
            "space_id": "",
        }
        assert [r["run_id"] for r in registry.list()] == ["run_live"]

    def test_no_store_zero_behavior_change(self):
        registry = self._registry(None)
        registry._runs["run_live"] = {
            "run_id": "run_live",
            "workflow_id": "wf_1",
            "workflow_name": "m",
            "status": "running",
            "created_at": time.time(),
            "space_id": "",
        }
        items = registry.list()
        assert len(items) == 1
        assert items[0]["run_id"] == "run_live"


class TestEndToEndWithApi:
    def test_runs_api_lists_history_after_restart(self, tmp_path, monkeypatch):
        # 全链路: run 终态落摘要 → 新 create_web_app 同 store_root (模拟重启) →
        # GET /api/runs 列表可见历史 run
        from fastapi.testclient import TestClient

        from fusion_cowork.server.web_api import create_web_app

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k399")
        store1 = RunStateStore(tmp_path / "run_states.db")
        store1.save_summary(_summary_record("run_done", workflow_name="delivery", source="web"))
        store1.close()

        app = create_web_app(store_root=tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/runs", headers={"Authorization": "Bearer k399"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["count"] >= 1
            row = next(r for r in body["runs"] if r["run_id"] == "run_done")
            assert row["status"] == "success"
            assert row["summary_row"] is True
