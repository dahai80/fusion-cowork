"""POST /api/trajectory/compile — flywheel compile endpoint (issue #623 follow-up).

Tests: auth, session_id required, path-traversal rejection, missing file 404,
snapshot mode, empty trajectory 422, success returns compiled workflow with
skip_if conditions, empty plan 422, model override passes through.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from fusion_cowork.engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeRegistry,
    NodeResult,
    NodeStatus,
    register_node,
)


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


STATIC_KEY = "test-static-key"
AUTH = {"Authorization": f"Bearer {STATIC_KEY}"}

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
        self.calls = []

    async def chat(self, messages, model="", temperature=None, max_tokens=None):
        self.calls.append({"model": model, "n": len(messages)})
        return self.reply


def _make_client(monkeypatch, tmp_path, trajectory_files=None, llm_reply=None):
    # full-suite runs may have cleared the registry after module import;
    # re-register the test nodes defensively
    NodeRegistry.register(_EchoNode, force=True)
    NodeRegistry.register(_SinkNode, force=True)
    monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
    from fusion_cowork.server import web_api
    from fusion_cowork.trajectory.recorder import DEFAULT_TRAJECTORY_DIR

    traj_dir = Path(DEFAULT_TRAJECTORY_DIR)
    traj_dir.mkdir(parents=True, exist_ok=True)
    for name, lines in (trajectory_files or {}).items():
        (traj_dir / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    stub = _StubLLM(llm_reply or "")
    gen = web_api.nl_generator if False else None  # never use module global
    from fusion_cowork.ai.nl_parser import NLWorkflowGenerator

    gen = NLWorkflowGenerator.__new__(NLWorkflowGenerator)
    gen.mlx = stub
    gen.model = ""
    app = web_api.create_web_app(dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store", nl_generator=gen)
    return TestClient(app), stub


class TestTrajectoryCompileAPI:
    def test_requires_auth(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", STATIC_KEY)
        from fusion_cowork.server import web_api

        app = web_api.create_web_app(dist_dir=tmp_path / "no-dist", store_root=tmp_path / "store")
        client = TestClient(app)
        resp = client.post("/api/trajectory/compile", json={"session_id": "s1"})
        assert resp.status_code in (401, 403)

    def test_session_or_snapshot_required(self, monkeypatch, tmp_path):
        client, _ = _make_client(monkeypatch, tmp_path)
        resp = client.post("/api/trajectory/compile", json={}, headers=AUTH)
        assert resp.status_code == 400

    def test_bad_session_id_rejected(self, monkeypatch, tmp_path):
        client, _ = _make_client(monkeypatch, tmp_path)
        resp = client.post("/api/trajectory/compile", json={"session_id": "../etc"}, headers=AUTH)
        assert resp.status_code == 400

    def test_missing_file_404(self, monkeypatch, tmp_path):
        client, _ = _make_client(monkeypatch, tmp_path)
        resp = client.post("/api/trajectory/compile", json={"session_id": "ghost"}, headers=AUTH)
        assert resp.status_code == 404

    def test_empty_trajectory_422(self, monkeypatch, tmp_path):
        client, _ = _make_client(
            monkeypatch, tmp_path, trajectory_files={"empty": [json.dumps({"payload": {"legacy_event": "other"}})]}
        )
        resp = client.post("/api/trajectory/compile", json={"session_id": "empty"}, headers=AUTH)
        assert resp.status_code == 422

    def test_snapshot_mode_compiles(self, monkeypatch, tmp_path):
        client, stub = _make_client(monkeypatch, tmp_path, llm_reply=_PLAN)
        snap = [{"node_name": "echo", "status": "success", "error": ""}]
        resp = client.post("/api/trajectory/compile", json={"snapshot": snap}, headers=AUTH)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["steps_used"] == 1
        assert body["workflow"]["nodes"]

    def test_success_returns_workflow_with_skip_if(self, monkeypatch, tmp_path):
        client, stub = _make_client(
            monkeypatch,
            tmp_path,
            trajectory_files={"sess1": [json.dumps(_REC)]},
            llm_reply=_PLAN,
        )
        resp = client.post("/api/trajectory/compile", json={"session_id": "sess1", "run_id": "run-1"}, headers=AUTH)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["plan"]["steps"]
        assert len(body["node_ids"]) == 2
        wf = body["workflow"]
        assert wf["nodes"]
        conds = [n.get("config", {}).get("skip_if") for n in wf["nodes"].values() if n.get("config", {}).get("skip_if")]
        assert conds, "compiled condition must land as skip_if"
        assert stub.calls, "one LLM condition-extraction call expected"

    def test_model_override_reaches_llm(self, monkeypatch, tmp_path):
        client, stub = _make_client(
            monkeypatch,
            tmp_path,
            trajectory_files={"sess2": [json.dumps(_REC)]},
            llm_reply=_PLAN,
        )
        resp = client.post("/api/trajectory/compile", json={"session_id": "sess2", "model": "my-model"}, headers=AUTH)
        assert resp.status_code == 200
        assert stub.calls[0]["model"] == "my-model"

    def test_empty_plan_422(self, monkeypatch, tmp_path):
        client, _ = _make_client(
            monkeypatch,
            tmp_path,
            trajectory_files={"sess3": [json.dumps(_REC)]},
            llm_reply=json.dumps({"steps": []}),
        )
        resp = client.post("/api/trajectory/compile", json={"session_id": "sess3"}, headers=AUTH)
        assert resp.status_code == 422
