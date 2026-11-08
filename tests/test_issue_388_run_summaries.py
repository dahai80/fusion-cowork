"""issue #388: 终态 run 摘要持久化 — source/labels 落库 + 终态摘要行 + 保留期清理。

- run_states schema v3: +source/labels 列 (v2 库幂等迁移)
- 终态先落 run_summaries 摘要行 (每步耗时/归因), 活跃行照删 —
  daemon 重启后 GET /api/runs/{id} 走摘要兜底, 下游按 run 时长对账
- prune_summaries 保留期 (默认 90 天) 清理; recover_persisted 启动时执行
"""

import time

from fusion_cowork.engine.run_persistence import SCHEMA_VERSION, RunStateStore


def _record(run_id: str, status: str = "success", **kw) -> dict:
    base = {
        "run_id": run_id,
        "workflow_id": "wf_1",
        "workflow_name": "t388",
        "status": status,
        "source": "pipeline",
        "labels": {"task": "9", "resume_of": "run_old"},
        "created_at": time.time() - 100,
        "started_at": time.time() - 90,
        "finished_at": time.time(),
        "error": "boom" if status == "failed" else None,
        "steps": [
            {"node_id": "n1", "node_name": "Fetch", "status": "success", "execution_time": 1.5, "attempt": 1},
            {"node_id": "n2", "node_name": "Deliver", "status": "success", "execution_time": 3.0, "attempt": 2},
        ],
    }
    base.update(kw)
    return base


class TestSchemaV3:
    def test_fresh_db_has_v3_columns_and_table(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(run_states)").fetchall()}
        assert {"source", "labels"} <= cols
        tables = {
            r["name"] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "run_summaries" in tables
        assert SCHEMA_VERSION == 3
        store.close()

    def test_v2_db_migrates_idempotent(self, tmp_path):
        # 手造 v2 库 (无 source/labels 列) → 打开即迁移, 数据保留
        import sqlite3

        p = tmp_path / "v2.db"
        conn = sqlite3.connect(str(p))
        conn.executescript(
            """
            CREATE TABLE run_states (
                run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL DEFAULT '',
                workflow_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'running',
                paused_reason TEXT NOT NULL DEFAULT '', workflow_def TEXT NOT NULL DEFAULT '{}',
                steps TEXT NOT NULL DEFAULT '[]', directives TEXT NOT NULL DEFAULT '[]',
                memory INTEGER NOT NULL DEFAULT 1, space_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0
            );
            INSERT INTO run_states (run_id, status) VALUES ('run_v2', 'paused');
            """
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        store = RunStateStore(p)
        rows = store.load_active()
        assert len(rows) == 1 and rows[0]["run_id"] == "run_v2"
        assert rows[0]["source"] == "" and rows[0]["labels"] == "{}"
        store.close()

    def test_save_persists_source_labels(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        rec = _record("run_a")
        rec["status"] = "paused"
        store.save(rec, paused_reason="manual")
        row = next(r for r in store.load_active() if r["run_id"] == "run_a")
        assert row["source"] == "pipeline"
        assert "task" in (row["labels"] or "")
        store.close()


class TestSummaries:
    def test_summary_roundtrip(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        store.save_summary(_record("run_s1"))
        s = store.load_summary("run_s1")
        assert s["source"] == "pipeline"
        assert s["labels"]["task"] == "9"
        assert s["status"] == "success"
        assert [st["node_id"] for st in s["steps"]] == ["n1", "n2"]
        assert s["steps"][1]["execution_time"] == 3.0
        assert s["steps"][0].get("input_data") is None and s["steps"][0].get("output_data") is None
        store.close()

    def test_summary_steps_drop_heavy_fields(self, tmp_path):
        rec = _record("run_s2")
        rec["steps"][0]["input_data"] = {"big": "x" * 5000}
        rec["steps"][0]["output_data"] = {"out": 1}
        store = RunStateStore(tmp_path / "rs.db")
        store.save_summary(rec)
        s = store.load_summary("run_s2")
        assert "input_data" not in s["steps"][0] and "output_data" not in s["steps"][0]
        store.close()

    def test_load_missing_and_list(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        assert store.load_summary("nope") is None
        store.save_summary(_record("run_l1", finished_at=time.time() - 10))
        store.save_summary(_record("run_l2", finished_at=time.time()))
        listed = store.load_summaries(limit=10)
        assert [r["run_id"] for r in listed] == ["run_l2", "run_l1"]
        store.close()

    def test_prune_retention(self, tmp_path):
        store = RunStateStore(tmp_path / "rs.db")
        old = _record("run_old", finished_at=time.time() - 91 * 86400)
        new = _record("run_new", finished_at=time.time())
        store.save_summary(old)
        store.save_summary(new)
        deleted = store.prune_summaries(90)
        assert deleted == 1
        assert store.load_summary("run_old") is None
        assert store.load_summary("run_new") is not None
        store.close()


class TestRegistryWiring:
    def _registry(self, tmp_path):
        from fusion_cowork.server.web_api import RunRegistry

        reg = RunRegistry.__new__(RunRegistry)
        reg._runs = {}
        reg.run_store = RunStateStore(tmp_path / "rs.db")
        return reg

    def test_get_falls_back_to_summary(self, tmp_path):
        reg = self._registry(tmp_path)
        reg.run_store.save_summary(_record("run_x"))
        got = reg.get("run_x")
        assert got is not None
        assert got["status"] == "success"
        assert got["source"] == "pipeline"
        assert got["labels"]["task"] == "9"
        assert got["summary_row"] is True
        assert got["steps"][0]["node_id"] == "n1"
        assert reg.get("missing") is None

    def test_active_record_wins_over_summary(self, tmp_path):
        reg = self._registry(tmp_path)
        reg.run_store.save_summary(_record("run_y", status="failed"))
        reg._runs["run_y"] = {"run_id": "run_y", "status": "running", "steps": []}
        got = reg.get("run_y")
        assert got["status"] == "running"
        assert "summary_row" not in got
