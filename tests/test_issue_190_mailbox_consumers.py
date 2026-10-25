"""issue #190 回归测试 — durable 团队信箱 + daemon 常驻消费池。

覆盖验收点:
1. 消息落 SQLite, 重启不丢 (新建实例读未 ack 消息);
2. at-least-once: 派发成功才 ack, 派发失败不 ack 且下一 tick 重投;
3. 常驻消费: 花名册 agent 自动消费, 无需外部 RPC 拉动; 本地发送推送唤醒;
4. 指标: consumed / replies_sent / depth / oldest_unacked_age 可观测, 计数不重复累加;
5. mailbox 表与 agent-studio durable_roster 同构 (跨仓互通协议)。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

AGENT_STUDIO_MAILBOX_SQL = """
    CREATE TABLE IF NOT EXISTS mailbox (
        message_id TEXT PRIMARY KEY,
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        team TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'message',
        body TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        acked_at REAL NOT NULL DEFAULT 0
    )
"""


class TestTeamMailbox:
    def test_send_inbox_ack_roundtrip(self, tmp_path):
        from fusion_cowork.orchestrator.team_mailbox import TeamMailbox

        mb = TeamMailbox(str(tmp_path / "team.db"))
        sent = mb.send("alice", "bob", "hello", kind="message", team="t1")
        unread = mb.inbox("bob")
        assert len(unread) == 1
        assert unread[0]["message_id"] == sent["message_id"]
        assert unread[0]["body"] == "hello"
        assert mb.ack("bob", sent["message_id"]) is True
        assert mb.inbox("bob") == []
        assert len(mb.inbox("bob", unread_only=False)) == 1

    def test_ack_wrong_recipient_refused(self, tmp_path):
        from fusion_cowork.orchestrator.team_mailbox import TeamMailbox

        mb = TeamMailbox(str(tmp_path / "team.db"))
        sent = mb.send("alice", "bob", "x")
        assert mb.ack("carol", sent["message_id"]) is False
        assert len(mb.inbox("bob")) == 1

    def test_inbox_isolated_per_recipient(self, tmp_path):
        from fusion_cowork.orchestrator.team_mailbox import TeamMailbox

        mb = TeamMailbox(str(tmp_path / "team.db"))
        mb.send("alice", "bob", "for bob")
        mb.send("alice", "carol", "for carol")
        assert [m["body"] for m in mb.inbox("bob")] == ["for bob"]
        assert [m["body"] for m in mb.inbox("carol")] == ["for carol"]

    def test_restart_keeps_unacked_messages(self, tmp_path):
        # 进程重启 = 新实例; 未 ack 消息仍在 (durable, 非内存队列)
        from fusion_cowork.orchestrator.team_mailbox import TeamMailbox

        path = str(tmp_path / "team.db")
        first = TeamMailbox(path)
        sent = first.send("alice", "bob", "survive restart")
        del first
        second = TeamMailbox(path)
        assert [m["message_id"] for m in second.inbox("bob")] == [sent["message_id"]]

    def test_body_truncated(self, tmp_path):
        from fusion_cowork.orchestrator.team_mailbox import TeamMailbox

        mb = TeamMailbox(str(tmp_path / "team.db"))
        sent = mb.send("alice", "bob", "x" * 30000)
        assert len(mb.inbox("bob")[0]["body"]) == 20000
        assert sent["message_id"]

    def test_send_requires_both_parties(self, tmp_path):
        from fusion_cowork.orchestrator.team_mailbox import TeamMailbox

        mb = TeamMailbox(str(tmp_path / "team.db"))
        with pytest.raises(ValueError):
            mb.send("", "bob", "x")
        with pytest.raises(ValueError):
            mb.send("alice", "", "x")

    def test_depth_and_age(self, tmp_path):
        from fusion_cowork.orchestrator.team_mailbox import TeamMailbox

        mb = TeamMailbox(str(tmp_path / "team.db"))
        assert mb.depth() == 0
        assert mb.oldest_unacked_age() == 0.0
        sent = mb.send("alice", "bob", "x")
        assert mb.depth() == 1
        assert mb.depth("bob") == 1
        assert mb.oldest_unacked_age("bob") >= 0.0
        mb.ack("bob", sent["message_id"])
        assert mb.depth() == 0
        assert mb.oldest_unacked_age() == 0.0

    def test_cross_repo_schema_compat(self, tmp_path):
        # agent-studio 侧用同构 SQL 写消息, cowork 侧可读可 ack
        from fusion_cowork.orchestrator.team_mailbox import TeamMailbox

        path = str(tmp_path / "shared.db")
        conn = sqlite3.connect(path)
        conn.execute(AGENT_STUDIO_MAILBOX_SQL)
        conn.execute(
            "INSERT INTO mailbox (message_id, sender, recipient, team, kind, body, created_at, acked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            ("msg_as_1", "studio-agent", "cowork-agent", "team", "message", "hi cowork", time.time()),
        )
        conn.commit()
        conn.close()

        mb = TeamMailbox(path)
        messages = mb.inbox("cowork-agent")
        assert [m["message_id"] for m in messages] == ["msg_as_1"]
        assert mb.ack("cowork-agent", "msg_as_1") is True

        conn = sqlite3.connect(path)
        acked_at = conn.execute("SELECT acked_at FROM mailbox WHERE message_id = 'msg_as_1'").fetchone()[0]
        conn.close()
        assert acked_at > 0

    def test_db_path_env_precedence(self, tmp_path, monkeypatch):
        from fusion_cowork.orchestrator.team_mailbox import resolve_db_path

        monkeypatch.delenv("FUSION_COWORK_TEAM_DB", raising=False)
        monkeypatch.delenv("FUSION_TEAM_DB", raising=False)
        default = resolve_db_path()
        monkeypatch.setenv("FUSION_TEAM_DB", str(tmp_path / "as.db"))
        assert resolve_db_path() == str(tmp_path / "as.db")
        monkeypatch.setenv("FUSION_COWORK_TEAM_DB", str(tmp_path / "own.db"))
        assert resolve_db_path() == str(tmp_path / "own.db")
        assert default


class TestCoworkRoster:
    def test_register_get_list_remove(self, tmp_path):
        from fusion_cowork.orchestrator.team_mailbox import CoworkRoster

        roster = CoworkRoster(str(tmp_path / "team.db"))
        row = roster.register("agent-a", "wf-1", name="Agent A", team="t1")
        assert row["agent_id"] == "agent-a"
        assert row["workflow_id"] == "wf-1"
        assert row["enabled"] == 1
        assert roster.get("agent-a")["name"] == "Agent A"
        roster.register("agent-b", "wf-2", enabled=False)
        assert len(roster.list()) == 2
        assert [r["agent_id"] for r in roster.list(enabled_only=True)] == ["agent-a"]
        assert roster.remove("agent-b") is True
        assert roster.remove("agent-b") is False
        assert roster.list() == [row]

    def test_register_requires_workflow(self, tmp_path):
        from fusion_cowork.orchestrator.team_mailbox import CoworkRoster

        roster = CoworkRoster(str(tmp_path / "team.db"))
        with pytest.raises(ValueError):
            roster.register("", "wf-1")
        with pytest.raises(ValueError):
            roster.register("agent-a", "")

    def test_register_does_not_pollute_agent_studio_roster_table(self, tmp_path):
        # 两个 daemon 共用一个库: cowork 只写 cowork_roster, 不抢 AS 的 roster 行
        from fusion_cowork.orchestrator.team_mailbox import CoworkRoster

        path = str(tmp_path / "shared.db")
        CoworkRoster(path).register("agent-a", "wf-1")
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "cowork_roster" in tables
        assert "roster" not in tables


class TestRunnerPool:
    def _pool(self, tmp_path, dispatch, get_run=None, reply=True):
        from fusion_cowork.orchestrator.runner_pool import RunnerPool
        from fusion_cowork.orchestrator.team_mailbox import CoworkRoster, TeamMailbox

        path = str(tmp_path / "team.db")
        roster = CoworkRoster(path)
        mailbox = TeamMailbox(path)
        roster.register("agent-a", "wf-1")
        return RunnerPool(roster=roster, mailbox=mailbox, dispatch=dispatch, get_run=get_run, reply=reply)

    async def test_dispatch_acks_and_counts(self, tmp_path):
        calls = []

        async def dispatch(agent, message):
            calls.append((agent["agent_id"], message["message_id"]))
            return "run_1"

        pool = self._pool(tmp_path, dispatch)
        pool.mailbox.send("alice", "agent-a", "do it")
        await pool.tick()

        assert len(calls) == 1
        assert pool.mailbox.inbox("agent-a") == []
        assert pool.metrics["consumed"] == 1
        assert pool.metrics["dispatch_errors"] == 0
        assert pool.metrics["per_agent"]["agent-a"]["consumed"] == 1

    async def test_dispatch_failure_keeps_message_unacked(self, tmp_path):
        state = {"fail": True}
        calls = []

        async def dispatch(agent, message):
            calls.append(message["message_id"])
            if state["fail"]:
                raise RuntimeError("engine down")
            return "run_1"

        pool = self._pool(tmp_path, dispatch)
        pool.mailbox.send("alice", "agent-a", "retry me")
        await pool.tick()
        assert pool.metrics["dispatch_errors"] == 1
        assert len(pool.mailbox.inbox("agent-a")) == 1

        state["fail"] = False
        await pool.tick()
        assert len(calls) == 2
        assert pool.metrics["consumed"] == 1
        assert pool.mailbox.inbox("agent-a") == []

    async def test_dispatch_without_run_id_not_acked(self, tmp_path):
        async def dispatch(agent, message):
            return ""

        pool = self._pool(tmp_path, dispatch)
        pool.mailbox.send("alice", "agent-a", "x")
        await pool.tick()
        assert pool.metrics["dispatch_errors"] == 1
        assert len(pool.mailbox.inbox("agent-a")) == 1

    async def test_in_flight_message_not_redispatched(self, tmp_path):
        calls = []

        async def dispatch(agent, message):
            calls.append(message["message_id"])
            return "run_1"

        async def get_run(run_id):
            return {"status": "running"}

        pool = self._pool(tmp_path, dispatch, get_run=get_run)
        sent = pool.mailbox.send("alice", "agent-a", "x")
        await pool.tick()
        # 对端已经把 ack 抹掉 (或消息重复可见) 也不重复派发
        with sqlite3.connect(pool.mailbox.path) as conn:
            conn.execute("UPDATE mailbox SET acked_at = 0 WHERE message_id = ?", (sent["message_id"],))
        await pool.tick()
        assert len(calls) == 1
        assert sent["message_id"] in pool._in_flight

    async def test_reply_sent_on_success(self, tmp_path):
        async def dispatch(agent, message):
            return "run_1"

        async def get_run(run_id):
            return {"status": "success", "last_result": "finished ok"}

        pool = self._pool(tmp_path, dispatch, get_run=get_run)
        pool.mailbox.send("alice", "agent-a", "x")
        await pool.tick()
        await pool.tick()

        replies = pool.mailbox.inbox("alice")
        assert len(replies) == 1
        assert replies[0]["kind"] == "reply"
        assert "finished ok" in replies[0]["body"]
        assert pool.metrics["replies_sent"] == 1
        assert pool._in_flight == {}

    async def test_no_reply_on_failed_run(self, tmp_path):
        async def dispatch(agent, message):
            return "run_1"

        async def get_run(run_id):
            return {"status": "failed", "last_error": "boom"}

        pool = self._pool(tmp_path, dispatch, get_run=get_run)
        pool.mailbox.send("alice", "agent-a", "x")
        await pool.tick()
        await pool.tick()
        assert pool.mailbox.inbox("alice") == []
        assert pool.metrics["replies_sent"] == 0
        assert pool._in_flight == {}

    async def test_disabled_agent_not_consumed(self, tmp_path):
        from fusion_cowork.orchestrator.runner_pool import RunnerPool
        from fusion_cowork.orchestrator.team_mailbox import CoworkRoster, TeamMailbox

        path = str(tmp_path / "team.db")
        roster = CoworkRoster(path)
        roster.register("agent-a", "wf-1", enabled=False)
        calls = []

        async def dispatch(agent, message):
            calls.append(message["message_id"])
            return "run_1"

        pool = RunnerPool(roster=roster, mailbox=TeamMailbox(path), dispatch=dispatch)
        pool.mailbox.send("alice", "agent-a", "x")
        await pool.tick()
        assert calls == []
        assert len(pool.mailbox.inbox("agent-a")) == 1

    async def test_start_loop_consumes_on_push(self, tmp_path):
        calls = []

        async def dispatch(agent, message):
            calls.append(message["message_id"])
            return "run_1"

        pool = self._pool(tmp_path, dispatch)
        pool.poll_interval = 30.0
        await pool.start()
        try:
            pool.mailbox.send("alice", "agent-a", "pushed")
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.02)
        finally:
            await pool.stop()
        assert len(calls) == 1
        assert pool.metrics["consumed"] == 1

    async def test_status_reports_agents_and_metrics(self, tmp_path):
        async def dispatch(agent, message):
            return "run_1"

        pool = self._pool(tmp_path, dispatch)
        pool.mailbox.send("alice", "agent-a", "x")
        await pool.tick()
        status = pool.status()
        assert status["running"] is False
        assert status["consumers"] == 1
        assert status["agents"][0]["agent_id"] == "agent-a"
        assert status["agents"][0]["consumed"] == 1
        assert status["metrics"]["consumed"] == 1
        assert "mailbox_depth" in status["metrics"]
        assert "oldest_unacked_age_s" in status["metrics"]

    def test_metric_export_counts_deltas_only(self, monkeypatch):
        from fusion_cowork.orchestrator import runner_pool

        recorded = {"counters": {}, "histograms": {}}

        class _Counter:
            def __init__(self, name):
                self.name = name

            def add(self, value):
                recorded["counters"][self.name] = recorded["counters"].get(self.name, 0) + value

        class _Histogram:
            def __init__(self, name):
                self.name = name

            def record(self, value):
                recorded["histograms"][self.name] = value

        class _Meter:
            def create_counter(self, name, description=""):
                return _Counter(name)

            def create_histogram(self, name, description=""):
                return _Histogram(name)

        monkeypatch.setattr("fusion_cowork.observability.metrics.get_meter", lambda: _Meter())
        monkeypatch.setattr(runner_pool, "_instruments", {})
        monkeypatch.setattr(runner_pool, "_last_exported", {"consumed": 0, "replies_sent": 0})

        metrics = {"mailbox_depth": 3, "oldest_unacked_age_s": 12.5, "consumed": 2, "replies_sent": 1}
        runner_pool._export_metrics(metrics)
        runner_pool._export_metrics(metrics)
        metrics["consumed"] = 5
        runner_pool._export_metrics(metrics)

        assert recorded["counters"]["fusion_team_mailbox_consumed_total"] == 5
        assert recorded["counters"]["fusion_team_mailbox_replies_total"] == 1
        assert recorded["histograms"]["fusion_team_mailbox_depth"] == 3
        assert recorded["histograms"]["fusion_team_oldest_unacked_age_seconds"] == 12.5


STATIC_KEY = "test-key-190"


@pytest.fixture
def web_app(monkeypatch, tmp_path):
    from fusion_cowork.server import web_api

    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    monkeypatch.setenv("FUSION_INBOX_POLL_INTERVAL", "0.1")
    monkeypatch.setattr(web_api, "_module_key", lambda: "")
    app = web_api.create_web_app(
        dist_dir=tmp_path / "no-dist",
        store_root=tmp_path / "workflows",
        space_data_dir=tmp_path / "spaces",
        team_db=str(tmp_path / "team.db"),
    )
    return app


def _auth():
    return {"Authorization": f"Bearer {STATIC_KEY}"}


class TestTeamWebApi:
    def test_roster_and_mailbox_routes(self, web_app):
        from fastapi.testclient import TestClient

        with TestClient(web_app) as client:
            resp = client.post(
                "/api/team/roster",
                json={"agent_id": "agent-a", "workflow_id": "wf-1", "name": "A"},
                headers=_auth(),
            )
            assert resp.status_code == 201, resp.text
            assert resp.json()["workflow_id"] == "wf-1"

            listed = client.get("/api/team/roster", headers=_auth()).json()
            assert listed["count"] == 1
            assert listed["agents"][0]["agent_id"] == "agent-a"
            assert listed["db"].endswith("team.db")

            bad = client.post("/api/team/roster", json={"agent_id": "x"}, headers=_auth())
            assert bad.status_code == 400
            assert bad.json()["detail"]["code"] == "invalid_roster"

            sent = client.post(
                "/api/team/send",
                json={"sender": "alice", "recipient": "agent-a", "body": "hi"},
                headers=_auth(),
            )
            assert sent.status_code == 201, sent.text
            message_id = sent.json()["message_id"]

            inbox = client.get("/api/team/inbox", params={"agent_id": "agent-a"}, headers=_auth()).json()
            assert inbox["count"] == 1
            assert inbox["messages"][0]["message_id"] == message_id

            missing = client.post("/api/team/send", json={"sender": "alice", "body": "x"}, headers=_auth())
            assert missing.status_code == 400

            removed = client.delete("/api/team/roster/agent-a", headers=_auth())
            assert removed.status_code == 200
            assert client.delete("/api/team/roster/agent-a", headers=_auth()).status_code == 404

    def test_status_and_notify_and_pool_lifecycle(self, web_app):
        from fastapi.testclient import TestClient

        with TestClient(web_app) as client:
            status = client.get("/api/team/status", headers=_auth()).json()
            assert status["running"] is False
            assert status["consumers"] == 0

            started = client.post("/api/team/pool/start", headers=_auth()).json()
            assert started["running"] is True
            stopped = client.post("/api/team/pool/stop", headers=_auth()).json()
            assert stopped["running"] is False

            notify = client.post("/api/team/notify", json={"source": "as"}, headers=_auth()).json()
            assert notify["notified"] is True
            assert notify["running"] is False

    def test_startup_autostarts_pool_when_roster_enabled(self, web_app):
        from fastapi.testclient import TestClient

        web_app.state.team_roster.register("agent-a", "wf-1")
        with TestClient(web_app) as client:
            status = client.get("/api/team/status", headers=_auth()).json()
            assert status["running"] is True
            assert status["consumers"] == 1
        assert web_app.state.runner_pool.running is False

    def test_message_dispatch_runs_workflow_without_rpc_pull(self, web_app, tmp_path):
        from fastapi.testclient import TestClient

        started = []
        original_start = web_app.state.run_registry.start

        def _record_start(workflow_def, workflow_id="", workflow_name="", memory=True, message_input=None):
            started.append(
                {
                    "workflow_id": workflow_id,
                    "message_input": message_input,
                }
            )
            return {"run_id": f"run_fake_{len(started)}", "status": "running"}

        web_app.state.run_registry.start = _record_start
        try:
            web_app.state.run_registry._runs["run_fake_1"] = {
                "run_id": "run_fake_1",
                "status": "success",
                "result": {"summary": "done by agent"},
            }
            with TestClient(web_app) as client:
                created = client.post(
                    "/api/workflows",
                    json={"id": "wf-1", "name": "wf", "workflow": {"nodes": [], "edges": []}},
                    headers=_auth(),
                )
                assert created.status_code == 201, created.text
                client.post(
                    "/api/team/roster",
                    json={"agent_id": "agent-a", "workflow_id": "wf-1"},
                    headers=_auth(),
                )
                client.post("/api/team/pool/start", headers=_auth())
                sent = client.post(
                    "/api/team/send",
                    json={
                        "sender": "alice",
                        "recipient": "agent-a",
                        "body": json.dumps({"input": "make report"}),
                    },
                    headers=_auth(),
                )
                assert sent.status_code == 201
                message_id = sent.json()["message_id"]

                deadline = time.time() + 10
                while time.time() < deadline and not started:
                    time.sleep(0.05)
                assert started, "消费池未派发消息"

                payload = started[0]["message_input"]
                assert started[0]["workflow_id"] == "wf-1"
                assert payload["input"] == "make report"
                assert payload["sender"] == "alice"
                assert payload["message_id"] == message_id
                assert payload["team"] == ""

                deadline = time.time() + 10
                replies = []
                while time.time() < deadline:
                    replies = client.get("/api/team/inbox", params={"agent_id": "alice"}, headers=_auth()).json()[
                        "messages"
                    ]
                    if replies:
                        break
                    time.sleep(0.05)
                assert replies, "run 成功后应有回信"
                assert replies[0]["kind"] == "reply"
                assert "done by agent" in replies[0]["body"]

                status = client.get("/api/team/status", headers=_auth()).json()
                assert status["metrics"]["consumed"] >= 1
                assert status["metrics"]["replies_sent"] >= 1
        finally:
            web_app.state.run_registry.start = original_start


class TestCliTeamGroup:
    def test_commands_registered(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["team", "--help"])
        assert result.exit_code == 0, result.output
        for name in ("register", "roster", "remove", "send", "inbox", "status", "start", "stop"):
            assert name in result.output

    def test_register_roster_send_inbox_via_cli(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        monkeypatch.setenv("FUSION_COWORK_TEAM_DB", str(tmp_path / "team.db"))
        runner = CliRunner()

        reg = runner.invoke(cli, ["team", "register", "agent-a", "--workflow", "wf-1"])
        assert reg.exit_code == 0, reg.output
        assert "agent-a" in reg.output

        missing = runner.invoke(cli, ["team", "register", "agent-b"])
        assert missing.exit_code != 0

        roster = runner.invoke(cli, ["team", "roster"])
        assert "agent-a" in roster.output

        sent = runner.invoke(cli, ["team", "send", "--from", "alice", "--to", "agent-a", "hello"])
        assert sent.exit_code == 0, sent.output
        message_id = sent.output.split("已投递: ")[1].split(" ")[0]

        inbox = runner.invoke(cli, ["team", "inbox", "agent-a"])
        assert message_id in inbox.output
        assert "hello" in inbox.output

        removed = runner.invoke(cli, ["team", "remove", "agent-a"])
        assert removed.exit_code == 0
        assert "已移除" in removed.output
