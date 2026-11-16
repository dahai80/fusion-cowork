# issue #497: journal pending rows hygiene in /api/permissions/pending.
#
# Stale approval_journal rows (empty run_id — recorded before the run binding,
# or rows whose run reached a terminal state) used to surface in the merged
# pending list forever, polluting the approvals inbox and tripping cross-run
# consumers (nightly eval verify filters pendings by node_id/tool_name across
# ALL runs — one stale row fails every later verification).
# Fix: only rows bound to a non-terminal run are merged as source=journal.

import pytest

from fusion_cowork.engine.approval_journal import ApprovalJournal
from fusion_cowork.engine.workflow import WorkflowEngine

STATIC_KEY = "test-key-497"


@pytest.fixture
def env(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    engine = WorkflowEngine(permission_manager=None, hitl_pause=False)
    journal = ApprovalJournal(path=tmp_path / "journal.db")
    engine._approval_journal = journal
    app = web_api.create_web_app(engine=engine, dist_dir=tmp_path / "no-dist", store_root=tmp_path / "wf")
    return TestClient(app), journal


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


def _record(run_id, journal, node_id="n2", tool="approval_gate", run_id_value=""):
    journal.record_pending(
        action_id=run_id,
        run_id=run_id_value,
        node_id=node_id,
        node_name="Gate",
        tool_name=tool,
        workflow="wf",
        params={},
    )


class TestJournalPendingHygiene:
    def test_empty_run_id_row_dropped(self, env):
        tc, journal = env
        _record("local_stale1", journal, run_id_value="")
        resp = tc.get("/api/permissions/pending", headers=_auth())
        assert resp.status_code == 200
        aids = [a.get("action_id") for a in resp.json().get("approvals", [])]
        assert "local_stale1" not in aids

    def test_terminal_run_row_dropped(self, env):
        tc, journal = env
        _record("local_done1", journal, run_id_value="run_done_1")
        runs = tc.app.state.run_registry
        runs._runs["run_done_1"] = {"run_id": "run_done_1", "status": "failed"}
        resp = tc.get("/api/permissions/pending", headers=_auth())
        aids = [a.get("action_id") for a in resp.json().get("approvals", [])]
        assert "local_done1" not in aids

    def test_active_run_row_kept(self, env):
        tc, journal = env
        _record("local_live1", journal, run_id_value="run_live_1")
        runs = tc.app.state.run_registry
        runs._runs["run_live_1"] = {"run_id": "run_live_1", "status": "paused"}
        resp = tc.get("/api/permissions/pending", headers=_auth())
        rows = [a for a in resp.json().get("approvals", []) if a.get("action_id") == "local_live1"]
        assert len(rows) == 1
        assert rows[0]["source"] == "journal"
        assert rows[0]["execution_id"] == "run_live_1"

    def test_unknown_run_row_kept(self, env):
        # run record absent (pre-#399 history) — conservative: keep visible
        tc, journal = env
        _record("local_ghost1", journal, run_id_value="run_ghost_1")
        resp = tc.get("/api/permissions/pending", headers=_auth())
        aids = [a.get("action_id") for a in resp.json().get("approvals", [])]
        assert "local_ghost1" in aids
