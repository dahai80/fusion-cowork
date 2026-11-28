"""desk.workflow.compile — DeskRPC surface of the flywheel compile (#623).

Tests: shared compile_trajectory path (session/snapshot, error mapping to
{"error", "code"}), handler registration, snapshot-mode compile end-to-end
with a stubbed MLX client.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)
from fusion_cowork.server.desk_rpc import DeskRPCServer
from fusion_cowork.trajectory.compile import CompileError, compile_trajectory
from fusion_cowork.trajectory.recorder import DEFAULT_TRAJECTORY_DIR


@register_node
class _EchoNode(BaseNode):
    name = "echo"
    display_name = "echo"
    category = NodeCategory.IO
    description = "test: passthrough node"
    icon = "E"
    default_label = "echo"

    def __init__(self, node_id: str = "", config: NodeConfig = None):
        super().__init__(node_id=node_id, config=config or NodeConfig())

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"score": 0.9})


@register_node
class _SinkNode(BaseNode):
    name = "sink"
    display_name = "sink"
    category = NodeCategory.IO
    description = "test: terminal node"
    icon = "S"
    default_label = "sink"

    def __init__(self, node_id: str = "", config: NodeConfig = None):
        super().__init__(node_id=node_id, config=config or NodeConfig())

    async def execute(self, inputs: dict) -> NodeResult:
        return NodeResult(status=NodeStatus.SUCCESS, data={"done": True})


_PLAN = json.dumps(
    {
        "steps": [
            {"node": "echo", "params": {}, "when": None},
            {"node": "sink", "params": {}, "when": {"var": "echo.score", "gte": 0.5}},
        ]
    }
)

_REC = {
    "run_id": "run-1",
    "status": "success",
    "payload": {
        "legacy_event": "post_node_execute",
        "node_name": "echo",
        "data": {"result": {"status": "success", "data": {"score": 0.9}}},
    },
}


class _StubLLM:
    def __init__(self, reply: str):
        self.reply = reply

    async def chat(self, messages, model="", temperature=None, max_tokens=None):
        return self.reply


def _register_nodes():
    # full-suite runs may clear the registry after module import
    NodeRegistry.register(_EchoNode, force=True)
    NodeRegistry.register(_SinkNode, force=True)


def _server(tmp_path):
    return DeskRPCServer(sock_path=str(tmp_path / "desk.sock"))


class TestCompileTrajectoryShared:
    async def test_requires_session_or_snapshot(self):
        try:
            await compile_trajectory({}, _StubLLM(_PLAN), lambda n: None)
            raised = False
        except CompileError as e:
            raised = e.code == "bad_request"
        assert raised

    async def test_bad_session_id_rejected(self):
        try:
            await compile_trajectory({"session_id": "../etc"}, _StubLLM(_PLAN), lambda n: None)
            raised = False
        except CompileError as e:
            raised = e.code == "bad_request"
        assert raised

    async def test_missing_file_not_found(self):
        try:
            await compile_trajectory({"session_id": "ghost"}, _StubLLM(_PLAN), lambda n: None)
            raised = False
        except CompileError as e:
            raised = e.code == "not_found"
        assert raised

    async def test_snapshot_mode_compiles(self):
        _register_nodes()
        out = await compile_trajectory(
            {"snapshot": [{"node_name": "echo", "status": "success", "error": ""}]},
            _StubLLM(_PLAN),
            lambda name: NodeRegistry.create(name),
        )
        assert out["steps_used"] == 1
        assert out["node_ids"]
        assert out["workflow"]["nodes"]


class TestDeskHandler:
    async def test_handler_registered(self, tmp_path):
        srv = _server(tmp_path)
        assert "desk.workflow.compile" in srv._handlers

    async def test_snapshot_compile_end_to_end(self, tmp_path):
        _register_nodes()
        srv = _server(tmp_path)
        srv._get_mlx_client = lambda: _StubLLM(_PLAN)
        out = await srv._handlers["desk.workflow.compile"](
            {"snapshot": [{"node_name": "echo", "status": "success", "error": ""}]}
        )
        assert "error" not in out, out
        assert out["node_ids"]
        assert out["workflow"]["nodes"]

    async def test_error_maps_to_error_dict(self, tmp_path):
        _register_nodes()
        srv = _server(tmp_path)
        out = await srv._handlers["desk.workflow.compile"]({})
        assert out.get("error")
        assert out.get("code") == "bad_request"

    async def test_unknown_node_maps_to_error_dict(self, tmp_path, monkeypatch):
        _register_nodes()
        traj_dir = Path(DEFAULT_TRAJECTORY_DIR)
        traj_dir.mkdir(parents=True, exist_ok=True)
        (traj_dir / "desk-unk.jsonl").write_text(json.dumps(_REC) + "\n", encoding="utf-8")
        srv = _server(tmp_path)
        srv._get_mlx_client = lambda: _StubLLM(json.dumps({"steps": [{"node": "ghost_x"}]}))
        out = await srv._handlers["desk.workflow.compile"]({"session_id": "desk-unk"})
        assert out.get("code") == "unknown_node"
        (traj_dir / "desk-unk.jsonl").unlink()
