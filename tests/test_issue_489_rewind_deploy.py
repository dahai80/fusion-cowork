"""issue #489: pipeline stage rewind/resume (gate rejection semantics) + deployment executor.

Part 1 — rewind: gate 驳回后从回退点起重跑下游; 回退点前已消耗步骤快照续跑 (不重复执行);
幂等键 (run_id, node_id) — 产物 run 活跃期间重复调用返回同一 run; 每次回退落历史
(who/when/which rule) 到原 run rewinds 字段。gate 语义 (驳回到哪个节点) 由调用方编码,
cowork 提供原语 + 审计记录。

Part 2 — deploy_service 节点: docker (pull/run) / launchd (plist bootstrap) / script
(安装脚本) 三种部署方式 + 健康检查轮询; 阶段链 (pull→start→healthcheck) 带状态/重试/
日志摘要; 成功后 endpoint 即可访问的应用端点。subprocess 与健康轮询走模块级 seam,
测试零网络零子进程。
"""

from __future__ import annotations

import pytest

from fusion_cowork.nodes import import_all_nodes

import_all_nodes()

STATIC_KEY = "test-key-489"

WF_DEF = {
    "name": "wf-489",
    "nodes": [
        {"id": "prd", "name": "merge", "config": {}},
        {"id": "gate_a", "name": "merge", "config": {}},
        {"id": "coding", "name": "merge", "config": {}},
        {"id": "tests", "name": "merge", "config": {}},
        {"id": "gate_b", "name": "merge", "config": {}},
    ],
    "edges": [
        {"source_id": "prd", "target_id": "gate_a"},
        {"source_id": "gate_a", "target_id": "coding"},
        {"source_id": "coding", "target_id": "tests"},
        {"source_id": "tests", "target_id": "gate_b"},
    ],
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    web_api._WEB_DIR = tmp_path / "web"
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
    )
    return TestClient(app), app


def _headers():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


def _wait_terminal(tc, run_id, timeout=8.0):
    import time

    for _ in range(int(timeout * 20)):
        r = tc.get(f"/api/runs/{run_id}", headers=_headers())
        if r.status_code == 200 and r.json()["status"] not in ("running", "pending"):
            return r.json()
        time.sleep(0.05)
    raise AssertionError("run 未在超时内进入终态")


class TestRewind:
    def test_rewind_at_gate_reruns_downstream_snapshots_before(self, client):
        # gate B 驳回 → rewind 到 coding: prd/gate_a 快照续跑, coding→gate_b 真正重执行
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rw489"}, headers=_headers())
        assert r.status_code == 202, r.text
        orig_id = r.json()["run_id"]
        orig = _wait_terminal(tc, orig_id)
        assert orig["status"] == "success" and len(orig["steps"]) == 5

        r = tc.post(
            f"/api/runs/{orig_id}/rewind",
            json={"node_id": "coding", "rule": "gate_b_reject", "actor": "reviewer-1"},
            headers=_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        new_id = body["run_id"]
        assert body["rewind_of"] == orig_id
        assert body["resumed_from"] == "coding"
        assert body["idempotent"] is False
        assert body["snapshot_steps"] == 2  # prd + gate_a 已消耗, 免重跑

        rec = _wait_terminal(tc, new_id)
        assert rec["status"] == "success"
        assert [s["node_id"] for s in rec["steps"]] == ["prd", "gate_a", "coding", "tests", "gate_b"]
        # 审计链: 新 run labels 带回退规则/操作人
        assert rec["labels"]["rewind_of"] == orig_id
        assert rec["labels"]["rewind_rule"] == "gate_b_reject"
        assert rec["labels"]["rewind_actor"] == "reviewer-1"
        # 原 run 落回退历史 (who/when/which rule)
        orig_after = tc.get(f"/api/runs/{orig_id}", headers=_headers()).json()
        rewinds = orig_after["rewinds"]
        assert len(rewinds) == 1
        assert rewinds[0]["node_id"] == "coding"
        assert rewinds[0]["rule"] == "gate_b_reject"
        assert rewinds[0]["actor"] == "reviewer-1"
        assert rewinds[0]["new_run_id"] == new_id
        assert rewinds[0]["at"] > 0

    def test_rewind_idempotent_while_resumed_run_active(self, client, monkeypatch):
        # 同键 (run_id, node_id) 重复 rewind: 产物 run 活跃期间返回同一 run, 不再派生
        tc, app = client
        registry = app.state.run_registry
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rw489i"}, headers=_headers())
        orig_id = r.json()["run_id"]
        _wait_terminal(tc, orig_id)

        # 让产物 run 挂在 running: rewind 走 HTTP (start 需事件循环), 完成后手工置活跃态模拟仍在执行
        r = tc.post(
            f"/api/runs/{orig_id}/rewind", json={"node_id": "coding", "rule": "r1", "actor": "a1"}, headers=_headers()
        )
        assert r.status_code == 200, r.text
        first = r.json()
        assert first["idempotent"] is False
        resumed_id = first["run_id"]
        # 手工把产物 run 置 running (模拟仍在执行)
        registry._runs[resumed_id]["status"] = "running"

        second = tc.post(
            f"/api/runs/{orig_id}/rewind", json={"node_id": "coding", "rule": "r1", "actor": "a1"}, headers=_headers()
        ).json()
        assert second["idempotent"] is True
        assert second["run_id"] == resumed_id
        # 幂等命中不再追加 rewinds 历史
        assert len(registry._runs[orig_id]["rewinds"]) == 1
        # 不同节点键 → 非幂等, 派生新 run
        third = tc.post(
            f"/api/runs/{orig_id}/rewind", json={"node_id": "tests", "rule": "r2", "actor": "a2"}, headers=_headers()
        ).json()
        assert third["idempotent"] is False
        assert third["run_id"] != resumed_id
        assert len(registry._runs[orig_id]["rewinds"]) == 2

    def test_rewind_unknown_node_400_and_missing_run_404(self, client):
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rw489u"}, headers=_headers())
        orig_id = r.json()["run_id"]
        _wait_terminal(tc, orig_id)
        r = tc.post(f"/api/runs/{orig_id}/rewind", json={"node_id": "ghost"}, headers=_headers())
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "unknown_node"
        r = tc.post("/api/runs/run_missing/rewind", json={"node_id": "coding"}, headers=_headers())
        assert r.status_code == 404
        r = tc.post(f"/api/runs/{orig_id}/rewind", json={}, headers=_headers())
        assert r.status_code == 400

    def test_rewind_not_terminal_409(self, client):
        tc, app = client
        registry = app.state.run_registry
        registry._runs["run_489_live"] = {
            "run_id": "run_489_live",
            "workflow_id": "wf-489",
            "workflow_name": "wf-489",
            "status": "running",
            "created_at": 1.0,
            "finished_at": None,
            "steps": [],
            "result": None,
            "error": None,
            "workflow_def": WF_DEF,
        }
        r = tc.post("/api/runs/run_489_live/rewind", json={"node_id": "coding"}, headers=_headers())
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "not_terminal"

    def test_rewind_never_executed_node_full_rerun(self, client):
        # 回退点取首节点 → 快照 0, 全量重跑 (gate A 驳回重审场景)
        tc, app = client
        r = tc.post("/api/runs", json={"workflow": WF_DEF, "name": "rw489f"}, headers=_headers())
        orig_id = r.json()["run_id"]
        _wait_terminal(tc, orig_id)
        r = tc.post(f"/api/runs/{orig_id}/rewind", json={"node_id": "prd", "rule": "gate_a_reject"}, headers=_headers())
        assert r.status_code == 200
        assert r.json()["snapshot_steps"] == 0


# ── Part 2: deploy_service 节点 ──


def _node(params):
    from fusion_cowork.engine.node import NodeConfig, NodeRegistry

    return NodeRegistry.create("deploy_service", node_id="nd", config=NodeConfig(params=params))


def _patch_cmd(monkeypatch, script):
    """script: callable(cmd) -> (rc, output); patches deploy_node._run_cmd."""
    import fusion_cowork.nodes.ops.deploy_node as dn

    async def fake_run_cmd(cmd, timeout=600.0):
        return script(cmd)

    monkeypatch.setattr(dn, "_run_cmd", fake_run_cmd)


def _patch_health(monkeypatch, ok=True, detail="HTTP 200"):
    import fusion_cowork.nodes.ops.deploy_node as dn

    async def fake_poll(url, timeout_s=120.0, interval_s=2.0):
        return ok, detail

    monkeypatch.setattr(dn, "_poll_health", fake_poll)


class TestDeployService:
    def test_registration_and_high_risk(self):
        from fusion_cowork.engine.node import NodeRegistry
        from fusion_cowork.engine.permission_manager import HIGH_RISK_NODES

        assert NodeRegistry.get("deploy_service") is not None
        assert "deploy_service" in HIGH_RISK_NODES

    async def test_docker_happy_path(self, monkeypatch):
        cmds = []

        def script(cmd):
            cmds.append(cmd)
            return 0, f"ran: {cmd[:40]}\nline2"

        _patch_cmd(monkeypatch, script)
        _patch_health(monkeypatch, ok=True, detail="HTTP 200 (http://127.0.0.1:8080/health)")
        node = _node(
            {
                "target": "docker",
                "image": "site/app:latest",
                "container_name": "site-app",
                "ports": "8080:8080",
                "env": {"FOO": "bar"},
                "healthcheck_url": "http://127.0.0.1:8080/health",
            }
        )
        res = await node.execute({})
        assert res.status.value == "success"
        data = res.data
        assert data["deployed"] is True
        assert data["endpoint"] == "http://127.0.0.1:8080/health"
        stages = [s["stage"] for s in data["stages"]]
        assert stages == ["pull", "start", "healthcheck"]
        # 幂等重部署: 先 rm 旧容器再 run
        assert any(cmd.startswith("docker pull site/app:latest") for cmd in cmds)
        assert any("docker rm -f site-app" in cmd for cmd in cmds)
        run_cmd = next(c for c in cmds if c.startswith("docker run -d"))
        assert "--name site-app" in run_cmd
        assert "-e FOO=bar" in run_cmd
        assert "-p 8080:8080" in run_cmd
        hc = data["stages"][-1]
        assert hc["status"] == "ok" and "HTTP 200" in hc["log"]

    async def test_docker_pull_failure_retries_then_fails(self, monkeypatch):
        attempts = {"pull": 0}

        def script(cmd):
            if cmd.startswith("docker pull"):
                attempts["pull"] += 1
                return 1, "pull error: manifest unknown"
            return 0, "ok"

        _patch_cmd(monkeypatch, script)
        _patch_health(monkeypatch)
        node = _node(
            {
                "target": "docker",
                "image": "site/app:missing",
                "healthcheck_url": "http://127.0.0.1:8080/health",
                "stage_retries": 2,
            }
        )
        res = await node.execute({})
        assert res.status.value == "failed"
        assert attempts["pull"] == 3  # 1 initial + 2 retries
        assert res.data["deployed"] is False
        assert res.data["stages"][-1]["stage"] == "pull"
        assert res.data["stages"][-1]["status"] == "failed"
        assert "pull" in res.error

    async def test_launchd_bootstrap(self, monkeypatch, tmp_path):
        plist = tmp_path / "app.plist"
        plist.write_text("<plist/>")
        cmds = []

        def script(cmd):
            cmds.append(cmd)
            return 0, "bootstrapped"

        _patch_cmd(monkeypatch, script)
        _patch_health(monkeypatch, ok=True)
        node = _node(
            {
                "target": "launchd",
                "plist_path": str(plist),
                "healthcheck_url": "http://127.0.0.1:9090/health",
            }
        )
        res = await node.execute({})
        assert res.status.value == "success"
        stages = {s["stage"]: s for s in res.data["stages"]}
        assert stages["pull"]["status"] == "ok"  # plist 校验通过
        assert "launchctl bootstrap" in cmds[0] and str(plist) in cmds[0]
        assert "launchctl load" in cmds[0]  # fallback in same command

    async def test_launchd_missing_plist_fails_fast(self, monkeypatch, tmp_path):
        ran = []

        def script(cmd):
            ran.append(cmd)
            return 0, "ok"

        _patch_cmd(monkeypatch, script)
        _patch_health(monkeypatch)
        node = _node(
            {
                "target": "launchd",
                "plist_path": str(tmp_path / "nope.plist"),
                "healthcheck_url": "http://127.0.0.1:9090/health",
            }
        )
        res = await node.execute({})
        assert res.status.value == "failed"
        assert res.data["stages"][0]["stage"] == "pull"
        assert "plist 不存在" in res.data["stages"][0]["error"]
        assert ran == []  # 未执行任何命令

    async def test_script_target_healthcheck_fail(self, monkeypatch):
        _patch_cmd(monkeypatch, lambda cmd: (0, "service started"))
        _patch_health(monkeypatch, ok=False, detail="deadline exceeded, last error: HTTP 503")
        node = _node(
            {
                "target": "script",
                "install_script": "sudo ./install.sh",
                "healthcheck_url": "http://127.0.0.1:7070/health",
            }
        )
        res = await node.execute({})
        assert res.status.value == "failed"
        stages = {s["stage"]: s for s in res.data["stages"]}
        assert stages["start"]["status"] == "ok"
        assert stages["healthcheck"]["status"] == "failed"
        assert res.data["endpoint"] == ""  # 健康不过不给端点 (避免假预览)

    async def test_validation_errors(self, monkeypatch):
        node = _node({"target": "docker", "healthcheck_url": "http://x/health"})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "image" in res.error

        node = _node({"healthcheck_url": ""})
        res = await node.execute({})
        assert res.status.value == "failed"

        node = _node({"healthcheck_url": "ftp://x"})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "http(s)" in res.error

        node = _node({"target": "script", "healthcheck_url": "http://x/health"})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "install_script" in res.error

        node = _node({"target": "launchd", "healthcheck_url": "http://x/health"})
        res = await node.execute({})
        assert res.status.value == "failed"
        assert "plist_path" in res.error

    async def test_install_script_from_upstream_input(self, monkeypatch):
        seen = {}

        def script(cmd):
            seen["cmd"] = cmd
            return 0, "ok"

        _patch_cmd(monkeypatch, script)
        _patch_health(monkeypatch, ok=True)
        node = _node({"target": "script", "healthcheck_url": "http://127.0.0.1:6060/health"})
        res = await node.execute({"install_script": "/opt/app/deploy.sh"})
        assert res.status.value == "success"
        assert seen["cmd"] == "/opt/app/deploy.sh"
