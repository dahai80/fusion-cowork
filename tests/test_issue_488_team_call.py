"""issue #488: team_call DAG node — drive agent-studio team members from workflows.

- run mode: teams.add_task -> teams.run_member -> poll teams.status to terminal
  (task done on the board = node success, task failed = node failure)
- add_task mode: board write only (DAG -> board blocked_by mirror, artifact_refs)
- run_member mode: run an existing task_id without creating a task
- lifecycle guard: inactive team state (paused/disbanded) rejects before any
  board mutation; RPC failure / timeout never mutates team state; T3 missing
  (JSON-RPC -32601) fails loud with a dispatcher hint
"""

import json

import httpx

from fusion_cowork.engine.node import NodeRegistry
from fusion_cowork.engine.permission_manager import HIGH_RISK_NODES
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ecosystem.team_node import TeamCallNode

import_all_nodes()


class _FakeTeamBoard:
    """In-memory teams.* JSON-RPC dispatcher (T3 contract stub)."""

    def __init__(self, allow_teams: bool = True):
        self.tasks: dict = {}
        self.next_id = 1
        self.state = "active"
        self.member_calls: list = []
        self.add_task_calls: list = []
        self.allow_teams = allow_teams

    def rpc(self, method: str, params: dict):
        if not self.allow_teams or not method.startswith("teams."):
            return None, {"code": -32601, "message": f"web rpc bridge rejected non-allowlisted method: {method}"}
        if method == "teams.status":
            return {"state": self.state, "tasks": {tid: dict(t) for tid, t in self.tasks.items()}}, None
        if method == "teams.add_task":
            self.add_task_calls.append(dict(params))
            tid = f"task-{self.next_id}"
            self.next_id += 1
            self.tasks[tid] = {
                "status": "todo",
                "summary": "",
                "artifact_refs": list(params.get("artifact_refs") or []),
            }
            return {"task_id": tid, "status": "todo"}, None
        if method == "teams.run_member":
            self.member_calls.append(dict(params))
            tid = str(params.get("task_id", ""))
            if tid not in self.tasks:
                return None, {"code": -32000, "message": f"unknown task: {tid}"}
            if params.get("member") == "ghost":
                return None, {"code": -32000, "message": "unknown member: ghost"}
            self.tasks[tid]["status"] = "in_progress"
            return {"ok": True}, None
        return None, {"code": -32601, "message": f"method not found: {method}"}


def _jsonrpc_handler(board: _FakeTeamBoard):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/rpc"
        body = json.loads(request.content)
        assert body.get("jsonrpc") == "2.0"
        result, error = board.rpc(str(body.get("method")), body.get("params") or {})
        payload: dict = {"jsonrpc": "2.0", "id": body.get("id")}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        return httpx.Response(200, json=payload)

    return handler


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.pop("transport", None)
        orig_init(self, *args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _node(params: dict) -> TeamCallNode:
    n = NodeRegistry.create("team_call", node_id="nt")
    n.config.params = params
    return n


class TestTeamCallRun:
    async def test_run_completes_task_done(self, monkeypatch):
        board = _FakeTeamBoard()

        def handler(request: httpx.Request) -> httpx.Response:
            resp = _jsonrpc_handler(board)(request)
            body = json.loads(request.content)
            # after run_member marks in_progress, next status poll sees done
            if body.get("method") == "teams.status" and board.member_calls:
                tid = next(iter(board.tasks))
                if board.tasks[tid]["status"] == "in_progress":
                    board.tasks[tid].update(status="done", summary="shipped the PRD", artifact_refs=["art-9"])
            return resp

        _patch_transport(monkeypatch, handler)
        res = await _node({"team": "alpha", "member": "ba", "task_title": "Write PRD"}).execute({})
        assert res.status.value == "success"
        assert res.data["status"] == "done"
        assert res.data["summary"] == "shipped the PRD"
        assert res.data["artifact_ref"] == "art-9"
        # acceptance: task marked done on the board
        assert all(t["status"] == "done" for t in board.tasks.values())

    async def test_run_board_failed_fails_node(self, monkeypatch):
        board = _FakeTeamBoard()

        def handler(request: httpx.Request) -> httpx.Response:
            resp = _jsonrpc_handler(board)(request)
            body = json.loads(request.content)
            if body.get("method") == "teams.status" and board.member_calls:
                tid = next(iter(board.tasks))
                if board.tasks[tid]["status"] == "in_progress":
                    board.tasks[tid].update(status="failed", summary="lint broke")
            return resp

        _patch_transport(monkeypatch, handler)
        res = await _node({"team": "alpha", "member": "ba", "task_title": "t"}).execute({})
        assert res.status.value == "failed"
        assert res.data["status"] == "failed"
        assert "lint broke" in res.error

    async def test_blocked_by_mirror_forwarded(self, monkeypatch):
        board = _FakeTeamBoard()
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        res = await _node(
            {
                "mode": "add_task",
                "team": "alpha",
                "task_title": "t",
                "blocked_by": ["task-9"],
                "artifact_refs": ["art-1"],
            }
        ).execute({})
        assert res.status.value == "success"
        assert board.add_task_calls[0]["blocked_by"] == ["task-9"]
        assert board.add_task_calls[0]["artifact_refs"] == ["art-1"]

    async def test_add_task_mode_no_member_call(self, monkeypatch):
        board = _FakeTeamBoard()
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        res = await _node({"mode": "add_task", "team": "alpha", "task_title": "mirror"}).execute({})
        assert res.status.value == "success"
        assert res.data["status"] == "todo"
        assert res.data["task_id"].startswith("task-")
        assert board.member_calls == []

    async def test_run_member_mode_uses_existing_task(self, monkeypatch):
        board = _FakeTeamBoard()
        board.tasks["task-77"] = {"status": "todo", "summary": "", "artifact_refs": []}

        def handler(request: httpx.Request) -> httpx.Response:
            resp = _jsonrpc_handler(board)(request)
            body = json.loads(request.content)
            if body.get("method") == "teams.status" and board.member_calls:
                if board.tasks["task-77"]["status"] == "in_progress":
                    board.tasks["task-77"].update(status="done", summary="existing task ran")
            return resp

        _patch_transport(monkeypatch, handler)
        res = await _node({"mode": "run_member", "team": "alpha", "member": "ba", "task_id": "task-77"}).execute({})
        assert res.status.value == "success"
        assert board.add_task_calls == []
        assert board.member_calls[0]["task_id"] == "task-77"

    async def test_inputs_fallbacks(self, monkeypatch):
        board = _FakeTeamBoard()
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        node = _node({"mode": "add_task", "task_title": "from inputs"})
        res = await node.execute({"team": "alpha", "blocked_by": ["t1"]})
        assert res.status.value == "success"
        assert board.add_task_calls[0]["team"] == "alpha"
        assert board.add_task_calls[0]["blocked_by"] == ["t1"]


class TestTeamCallGuards:
    async def test_t3_missing_fails_loud(self, monkeypatch):
        board = _FakeTeamBoard(allow_teams=False)
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        res = await _node({"team": "alpha", "member": "ba", "task_title": "t"}).execute({})
        assert res.status.value == "failed"
        assert "teams.*" in res.error
        assert "dispatcher" in res.error
        assert board.tasks == {}

    async def test_paused_team_rejected_before_mutation(self, monkeypatch):
        board = _FakeTeamBoard()
        board.state = "paused"
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        res = await _node({"team": "alpha", "member": "ba", "task_title": "t"}).execute({})
        assert res.status.value == "failed"
        assert "paused" in res.error
        assert board.add_task_calls == []
        assert board.member_calls == []

    async def test_run_member_rpc_error_no_board_corruption(self, monkeypatch):
        board = _FakeTeamBoard()
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        res = await _node({"team": "alpha", "member": "ghost", "task_title": "t"}).execute({})
        assert res.status.value == "failed"
        assert "unknown member: ghost" in res.error
        # task was created then run failed — board keeps it as todo, no corruption
        assert len(board.tasks) == 1
        assert next(iter(board.tasks.values()))["status"] == "todo"

    async def test_state_flip_midrun_preserves_task(self, monkeypatch):
        board = _FakeTeamBoard()

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body.get("method") == "teams.status" and board.member_calls:
                board.state = "paused"
            return _jsonrpc_handler(board)(request)

        _patch_transport(monkeypatch, handler)
        res = await _node({"team": "alpha", "member": "ba", "task_title": "t"}).execute({})
        assert res.status.value == "failed"
        assert res.data["task_id"]
        # mid-run pause: task stays in_progress on the board, never marked done by the DAG side
        assert next(iter(board.tasks.values()))["status"] == "in_progress"

    async def test_timeout_detaches_with_task_id(self, monkeypatch):
        board = _FakeTeamBoard()
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        res = await _node(
            {"team": "alpha", "member": "ba", "task_title": "t", "timeout": 0.15, "poll_interval": 0.05}
        ).execute({})
        assert res.status.value == "failed"
        assert "超时" in res.error
        assert res.data["detached"] is True
        assert res.data["task_id"].startswith("task-")

    async def test_missing_team_fails_no_rpc(self, monkeypatch):
        board = _FakeTeamBoard()
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        res = await _node({"member": "ba"}).execute({})
        assert res.status.value == "failed"
        assert "team" in res.error
        assert board.add_task_calls == []

    async def test_missing_member_fails(self, monkeypatch):
        board = _FakeTeamBoard()
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        res = await _node({"team": "alpha", "task_title": "t"}).execute({})
        assert res.status.value == "failed"
        assert "member" in res.error

    async def test_missing_task_title_fails(self, monkeypatch):
        board = _FakeTeamBoard()
        _patch_transport(monkeypatch, _jsonrpc_handler(board))
        res = await _node({"team": "alpha", "member": "ba"}).execute({})
        assert res.status.value == "failed"
        assert "task_title" in res.error

    async def test_run_member_requires_task_id(self, monkeypatch):
        res = await _node({"mode": "run_member", "team": "alpha", "member": "ba"}).execute({})
        assert res.status.value == "failed"
        assert "task_id" in res.error


class TestRegistration:
    def test_registered_and_aliases(self):
        assert NodeRegistry.get("team_call") is not None
        from fusion_cowork import NODE_NAME_ALIASES

        assert NODE_NAME_ALIASES.get("团队调用") == "team_call"
        assert NODE_NAME_ALIASES.get("团队任务") == "team_call"

    def test_high_risk(self):
        assert "team_call" in HIGH_RISK_NODES
