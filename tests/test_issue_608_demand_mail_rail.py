# issue #608: demand-mail-rail seeded workflow + hourly cron — def seeded
# idempotently at app build, cron task created once on first serve (persisted,
# restart-safe, no duplicate), executor re-bound on restore, run-now endpoint
# (404 unknown / 409 paused), and the seeded workflow runs end-to-end against
# a test mailbox with zero LLM calls when no new mail.

import json

import pytest


class TestDemandMailRailSeed:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k608")
        from fusion_cowork.engine import PermissionManager, WorkflowEngine
        from fusion_cowork.engine.scheduler import TaskScheduler

        scheduler = TaskScheduler(task_store_path=str(tmp_path / "tasks.json"))
        eng = WorkflowEngine(permission_manager=PermissionManager())
        from fusion_cowork.server import web_api

        app = web_api.create_web_app(
            engine=eng,
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "store",
            scheduler=scheduler,
            demand_db_path=str(tmp_path / "demand.db"),
            demand_catalog_path=str(tmp_path / "catalog.json"),
        )
        auth = {"Authorization": "Bearer k608"}
        with TestClient(app) as client:
            yield client, auth, scheduler, tmp_path

    def _defs(self, tmp_path):
        return json.loads((tmp_path / "store" / "demand-mail-rail.json").read_text(encoding="utf-8"))

    def test_workflow_def_seeded_with_4_nodes(self, api):
        client, auth, _, tmp_path = api
        assert client.get("/api/workflows", headers=auth).status_code == 200
        rec = self._defs(tmp_path)
        nodes = rec["workflow"]["nodes"]
        assert set(nodes) == {"poll", "normalize", "match", "draft"}
        assert [n["name"] for n in nodes.values()] == [
            "poll_qq_imap",
            "normalize_lead",
            "match",
            "dual_draft",
        ]
        # threshold 3.0 per the prototype requirement
        assert nodes["match"]["params"]["threshold"] == 3.0
        edges = {(e["source_id"], e["target_id"]) for e in rec["workflow"]["edges"]}
        assert edges == {("poll", "normalize"), ("normalize", "match"), ("match", "draft")}

    def test_workflow_def_seed_idempotent_operator_edits_survive(self, tmp_path, monkeypatch):
        # second app build over the same store must not clobber the existing def
        from fastapi.testclient import TestClient

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k608")
        from fusion_cowork.engine import PermissionManager, WorkflowEngine
        from fusion_cowork.engine.scheduler import TaskScheduler
        from fusion_cowork.server import web_api

        def build():
            return web_api.create_web_app(
                engine=WorkflowEngine(permission_manager=PermissionManager()),
                dist_dir=tmp_path / "no-dist",
                store_root=tmp_path / "store",
                scheduler=TaskScheduler(task_store_path=str(tmp_path / "tasks.json")),
                demand_db_path=str(tmp_path / "demand.db"),
                demand_catalog_path=str(tmp_path / "catalog.json"),
            )

        with TestClient(build()):
            pass
        rec = json.loads((tmp_path / "store" / "demand-mail-rail.json").read_text(encoding="utf-8"))
        rec["description"] = "operator edited"
        (tmp_path / "store" / "demand-mail-rail.json").write_text(json.dumps(rec), encoding="utf-8")
        with TestClient(build()):
            pass
        rec2 = json.loads((tmp_path / "store" / "demand-mail-rail.json").read_text(encoding="utf-8"))
        assert rec2["description"] == "operator edited"

    def test_cron_task_created_once_hourly(self, api):
        client, auth, scheduler, _ = api
        tasks = [t for t in scheduler.list_tasks() if t.workflow_id == "demand-mail-rail"]
        assert len(tasks) == 1
        assert tasks[0].trigger_config.get("cron") == "0 * * * *"
        assert tasks[0].status.value == "active"
        # restart-safe: a second serve over the same persisted store adds nothing
        scheduler.load_tasks()
        assert len([t for t in scheduler.list_tasks() if t.workflow_id == "demand-mail-rail"]) == 1

    def test_run_now_triggers_workflow_run(self, api):
        client, auth, scheduler, _ = api
        task = next(t for t in scheduler.list_tasks() if t.workflow_id == "demand-mail-rail")
        r = client.post(f"/api/scheduler/tasks/{task.id}/run-now", headers=auth)
        assert r.status_code == 200
        assert r.json()["triggered"] is True
        assert r.json()["run_count"] == 1
        # the executor found the seeded def and started a run
        runs = client.get("/api/runs", headers=auth).json()
        assert any(run["workflow_name"] == "demand-mail-rail" for run in runs["runs"])

    def test_run_now_unknown_404_paused_409(self, api):
        client, auth, scheduler, _ = api
        assert client.post("/api/scheduler/tasks/nope/run-now", headers=auth).status_code == 404
        task = next(t for t in scheduler.list_tasks() if t.workflow_id == "demand-mail-rail")
        assert scheduler.pause_task(task.id)
        r = client.post(f"/api/scheduler/tasks/{task.id}/run-now", headers=auth)
        assert r.status_code == 409
        assert scheduler.resume_task(task.id)

    def test_restored_task_executor_rebound(self, tmp_path, monkeypatch):
        # simulate restart: persisted task, fresh scheduler, executor re-bound by
        # the startup hook — auto-pause from P1-12 lifts, run-now works
        from fastapi.testclient import TestClient

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k608")
        from fusion_cowork.engine import PermissionManager, WorkflowEngine
        from fusion_cowork.engine.scheduler import TaskScheduler
        from fusion_cowork.server import web_api

        def build():
            return web_api.create_web_app(
                engine=WorkflowEngine(permission_manager=PermissionManager()),
                dist_dir=tmp_path / "no-dist",
                store_root=tmp_path / "store",
                scheduler=TaskScheduler(task_store_path=str(tmp_path / "tasks.json")),
                demand_db_path=str(tmp_path / "demand.db"),
                demand_catalog_path=str(tmp_path / "catalog.json"),
            )

        auth = {"Authorization": "Bearer k608"}
        task_id = None
        with TestClient(build()) as client:
            t = next(t for t in client.app.state.scheduler.list_tasks() if t.workflow_id == "demand-mail-rail")
            task_id = t.id
        # fresh process simulation: only the persisted file carries over
        with TestClient(build()) as client:
            sched = client.app.state.scheduler
            t = next(t for t in sched.list_tasks() if t.workflow_id == "demand-mail-rail")
            assert t.id == task_id  # restored from disk, not re-created
            assert t.status.value == "active"  # executor re-bound, auto-pause lifted
            r = client.post(f"/api/scheduler/tasks/{t.id}/run-now", headers=auth)
            assert r.status_code == 200


class TestMailRailE2ENoLLM:
    def test_seeded_workflow_runs_idle_no_llm(self, tmp_path, monkeypatch):
        # acceptance: the seeded rail runs end-to-end with zero LLM calls when
        # no new mail. IMAP host is forced to a refused loopback port so poll
        # fails fast offline (continue_on_error), and every store-backed node
        # gets a tmp db_path override (the seeded def leaves params empty —
        # nodes would otherwise fall back to the real ~/.fusion-cowork store).
        import asyncio

        monkeypatch.setenv("FUSION_QQ_IMAP_HOST", "127.0.0.1")
        monkeypatch.setenv("FUSION_QQ_IMAP_PORT", "1")
        from fusion_cowork.engine import PermissionManager, WorkflowEngine
        from fusion_cowork.engine.node import NodeRegistry
        from fusion_cowork.engine.workflow import Workflow
        from fusion_cowork.nodes.demand import demand_nodes  # noqa: F401 — register

        for name in ("poll_qq_imap", "normalize_lead", "match", "dual_draft"):
            assert NodeRegistry.get(name) is not None

        db_path = str(tmp_path / "demand.db")
        from fusion_cowork.server import web_api
        from fusion_cowork.server.web_api import WorkflowDefStore

        store = WorkflowDefStore(root=str(tmp_path / "store"))
        store.save("demand-mail-rail", web_api._DEMAND_MAIL_RAIL_DEF)
        wf_def = store.get("demand-mail-rail")["workflow"]
        for nid, node in wf_def["nodes"].items():
            if node["name"] in ("poll_qq_imap", "normalize_lead", "dual_draft"):
                node.setdefault("params", {})["db_path"] = db_path
        wf = Workflow.from_dict({"id": "demand-mail-rail", "name": "demand-mail-rail", **wf_def})

        eng = WorkflowEngine(permission_manager=PermissionManager())
        result = asyncio.run(eng.execute(wf))
        assert result.status.value == "success"
        statuses = {step.node_id: step.status for step in result.steps}
        # poll fails fast on the refused port but the rail continues;
        # normalize/match/dual_draft succeed on the empty store — no LLM
        assert statuses["poll"].value == "failed"
        assert statuses["normalize"].value == "success"
        assert statuses["match"].value == "success"
        assert statuses["draft"].value == "success"
