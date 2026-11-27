"""StudioRunNode unit tests — fusion-agent-studio HTTP node (issue #133).

Covers registration / aliases / param validation / async submit / sync graph execute / sync run
polling to terminal / timeout without cancel (detach, #351) / optional auth header / env overrides
(httpx MockTransport offline stub; never connects to a real agent-studio, per the offline test constraint).
"""

from __future__ import annotations

import json

import httpx
import pytest

import fusion_cowork.nodes.ecosystem  # noqa: F401  triggers @register_node
from fusion_cowork import NODE_NAME_ALIASES
from fusion_cowork.engine.node import NodeCategory, NodeConfig, NodeRegistry, NodeStatus
from fusion_cowork.engine.permission_manager import HIGH_RISK_NODES
from fusion_cowork.nodes.ecosystem.studio_node import StudioRunNode


def test_studio_run_registered():
    node = NodeRegistry.get("studio_run")
    assert node is StudioRunNode
    assert node.category == NodeCategory.FUSION_ECOSYSTEM
    assert node.name == "studio_run"


def test_studio_run_in_high_risk_nodes():
    assert "studio_run" in HIGH_RISK_NODES


def test_aliases_registered():
    assert NODE_NAME_ALIASES.get("运行Studio") == "studio_run"  # i18n: user-facing
    assert NODE_NAME_ALIASES.get("Studio运行") == "studio_run"  # i18n: user-facing


def test_params_schema_required_fields():
    node = StudioRunNode(node_id="s1")
    schema = node.get_params_schema()
    assert "input" in schema["required"]
    assert schema["properties"]["mode"]["default"] == "sync"
    assert schema["properties"]["timeout"]["default"] == 1800.0


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _make_node(graph_id="", agent_id="", **extra):
    params = {"input": "summarize inbox", **extra}
    if graph_id:
        params["graph_id"] = graph_id
    if agent_id:
        params["agent_id"] = agent_id
    return StudioRunNode(node_id="s1", config=NodeConfig(params=params))


@pytest.mark.asyncio
async def test_missing_input_fails(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(500))
    node = StudioRunNode(node_id="s1", config=NodeConfig(params={"graph_id": "g1"}))
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "input" in result.error


@pytest.mark.asyncio
async def test_missing_target_fails(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(500))
    node = StudioRunNode(node_id="s1", config=NodeConfig(params={"input": "hi"}))
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "graph_id" in result.error


@pytest.mark.asyncio
async def test_both_targets_fails(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(500))
    node = _make_node(graph_id="g1", agent_id="a1")
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "二选一" in result.error  # i18n: user-facing


@pytest.mark.asyncio
async def test_async_submit_returns_run_id(monkeypatch):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/v1/runs"
        payload = json.loads(request.content)
        assert payload["goal"] == "summarize inbox"
        assert payload["agent_id"] == "a1"
        return httpx.Response(200, json={"run_id": "run-42", "status": "pending", "deduped": False})

    monkeypatch.delenv("FUSION_AGENT_STUDIO_API_KEY", raising=False)
    _patch_transport(monkeypatch, handler)
    node = _make_node(agent_id="a1", mode="async")
    result = await node.execute({})
    assert result.status == NodeStatus.SUCCESS
    assert result.data["run_id"] == "run-42"
    assert result.data["status"] == "pending"
    assert paths == ["/v1/runs"]


@pytest.mark.asyncio
async def test_sync_graph_execute_collects_events(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/graphs/g1/execute"
        payload = json.loads(request.content)
        # issue #135 F-1: upstream GraphExecuteRequest requires graph_id in the body
        assert payload["graph_id"] == "g1"
        assert payload["input_text"] == "summarize inbox"
        return httpx.Response(
            200,
            json={
                "session_id": "sess-9",
                "events": [{"type": "node_start"}, {"type": "node_end"}],
                "status": "completed",
            },
        )

    _patch_transport(monkeypatch, handler)
    node = _make_node(graph_id="g1")
    result = await node.execute({})
    assert result.status == NodeStatus.SUCCESS
    assert result.data["status"] == "completed"
    assert len(result.data["result"]["events"]) == 2


@pytest.mark.asyncio
async def test_sync_graph_error_status_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session_id": "s", "events": [], "status": "error"})

    _patch_transport(monkeypatch, handler)
    node = _make_node(graph_id="g1")
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "error" in result.error


@pytest.mark.asyncio
async def test_sync_agent_polls_until_completed(monkeypatch):
    calls = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs":
            payload = json.loads(request.content)
            assert payload["agent_id"] == "a1"
            return httpx.Response(200, json={"run_id": "run-7", "status": "pending", "deduped": False})
        if request.url.path == "/v1/runs/run-7":
            calls["poll"] += 1
            if calls["poll"] < 3:
                return httpx.Response(200, json={"run_id": "run-7", "status": "running"})
            return httpx.Response(200, json={"run_id": "run-7", "status": "completed"})
        if request.url.path == "/v1/runs/run-7/result":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-7",
                    "status": "completed",
                    "steps": [],
                    "artifacts": ["art-1"],
                    "last_result": {"events": 5},
                    "last_error": "",
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _patch_transport(monkeypatch, handler)
    node = _make_node(agent_id="a1", poll_interval=0.01, timeout=30.0)
    result = await node.execute({})
    assert result.status == NodeStatus.SUCCESS
    assert result.data["run_id"] == "run-7"
    assert result.data["status"] == "completed"
    assert result.data["result"]["artifacts"] == ["art-1"]
    assert calls["poll"] == 3


@pytest.mark.asyncio
async def test_sync_agent_failed_terminal_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs":
            return httpx.Response(200, json={"run_id": "run-8", "status": "pending", "deduped": False})
        if request.url.path == "/v1/runs/run-8":
            return httpx.Response(200, json={"run_id": "run-8", "status": "failed"})
        if request.url.path == "/v1/runs/run-8/result":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-8",
                    "status": "failed",
                    "steps": [],
                    "artifacts": [],
                    "last_result": {},
                    "last_error": "model timeout",
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _patch_transport(monkeypatch, handler)
    node = _make_node(agent_id="a9", poll_interval=0.01)
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "model timeout" in result.error


@pytest.mark.asyncio
async def test_sync_timeout_detaches_without_cancel(monkeypatch):
    # issue #351: timeout does not cancel — cancelling would discard artifacts a healthy run already produced;
    # the run stays upstream and remains recoverable by run_id
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs":
            return httpx.Response(200, json={"run_id": "run-9", "status": "pending", "deduped": False})
        if request.url.path == "/v1/runs/run-9":
            return httpx.Response(200, json={"run_id": "run-9", "status": "running"})
        if request.url.path.endswith("/cancel"):
            raise AssertionError("cancel must not be called on sync timeout (issue #351 detach semantics)")
        raise AssertionError(f"unexpected path {request.url.path}")

    _patch_transport(monkeypatch, handler)
    node = _make_node(agent_id="a2", poll_interval=0.01, timeout=0.05)
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "超时" in result.error  # i18n: user-facing
    assert "未取消" in result.error  # i18n: user-facing
    assert result.data["run_id"] == "run-9"
    assert result.data["detached"] is True


@pytest.mark.asyncio
async def test_default_timeout_env_override(monkeypatch):
    # issue #351: FUSION_COWORK_STUDIO_RUN_TIMEOUT applies when the param is absent (small value here triggers the timeout path)
    monkeypatch.setenv("FUSION_COWORK_STUDIO_RUN_TIMEOUT", "0.05")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs":
            return httpx.Response(200, json={"run_id": "run-10", "status": "pending", "deduped": False})
        if request.url.path == "/v1/runs/run-10":
            return httpx.Response(200, json={"run_id": "run-10", "status": "running"})
        raise AssertionError(f"unexpected path {request.url.path}")

    _patch_transport(monkeypatch, handler)
    node = _make_node(agent_id="a3", poll_interval=0.01)
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "超时" in result.error  # i18n: user-facing
    assert result.data["detached"] is True


@pytest.mark.asyncio
async def test_api_key_header_sent_when_configured(monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "studio-key-1")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"run_id": "run-11", "status": "pending", "deduped": False})

    _patch_transport(monkeypatch, handler)
    node = _make_node(agent_id="a4", mode="async")
    result = await node.execute({})
    assert result.status == NodeStatus.SUCCESS
    assert seen["key"] == "studio-key-1"


@pytest.mark.asyncio
async def test_no_api_key_header_when_unset(monkeypatch):
    monkeypatch.delenv("FUSION_AGENT_STUDIO_API_KEY", raising=False)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"run_id": "run-12", "status": "pending", "deduped": False})

    _patch_transport(monkeypatch, handler)
    node = _make_node(agent_id="a5", mode="async")
    result = await node.execute({})
    assert result.status == NodeStatus.SUCCESS
    assert seen["key"] is None


@pytest.mark.asyncio
async def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_URL", "http://studio.lan:12442")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["port"] = request.url.port
        return httpx.Response(200, json={"run_id": "run-13", "status": "pending", "deduped": False})

    _patch_transport(monkeypatch, handler)
    node = _make_node(agent_id="a6", mode="async")
    result = await node.execute({})
    assert result.status == NodeStatus.SUCCESS
    assert seen["host"] == "studio.lan"
    assert seen["port"] == 12442


@pytest.mark.asyncio
async def test_upstream_http_error_fails_with_body(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="graph not found")

    _patch_transport(monkeypatch, handler)
    node = _make_node(agent_id="a7", mode="async")
    result = await node.execute({})
    assert result.status == NodeStatus.FAILED
    assert "404" in result.error
