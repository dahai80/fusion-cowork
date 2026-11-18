"""issue #517: decision gate node — fusion-mlx typed-decision lane for node gating.

- laya success paths (/v1/decisions state+questions, /v1/laya/decide preset) return
  typed answers with engine=laya and zero generation tokens.
- noul / low-confidence / transport error / timeout fall back to a single LLM turn
  (conservative default); fallback usage is recorded in decision_meta.tokens.
- fallback disabled or missing model fails visibly; state is compacted to fit the
  decision head's max_len 1024.
"""

from __future__ import annotations

import json

import httpx
import pytest

from fusion_cowork.engine.node import NodeRegistry, NodeStatus
from fusion_cowork.engine.workflow import WorkflowEngine
from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ecosystem import decision_gate_node


def _client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    return factory


def _laya_response_handler(responses):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        path = request.url.path
        if path == "/v1/decisions" and "state" in body:
            return httpx.Response(200, json=responses)
        if path == "/v1/laya/decide" and "preset" in body:
            return httpx.Response(200, json=responses)
        return httpx.Response(500, json={"error": f"unexpected payload for {path}"})

    return handler


def _make_node(params: dict) -> object:
    node = NodeRegistry.create("decision_gate")
    node.config.params = params
    return node


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    import_all_nodes()


GOOD_ANSWERS = {
    "answers": {
        "escalate": {"type": "choice", "confidence": 0.91, "action": {"act_probability": 0.91}},
    }
}


class TestLayaSuccess:
    async def test_decisions_route(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=GOOD_ANSWERS)

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))
        node = _make_node(
            {"state": {"step": "review", "risk": "low"}, "questions": [{"id": "escalate", "type": "choice"}]}
        )
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["answers"]["escalate"]["confidence"] == 0.91
        meta = result.data["decision_meta"]
        assert meta["engine"] == "laya" and meta["route"] == "decisions"
        assert meta["tokens"] == {"prompt_tokens": 0, "completion_tokens": 0}
        assert meta["tokens_saved_vs_llm_turn"] is True
        assert seen["url"].endswith("/v1/decisions")
        assert seen["body"]["state"] == json.dumps(
            {"risk": "low", "step": "review"}, ensure_ascii=False, sort_keys=True
        )

    async def test_laya_decide_preset_route(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=GOOD_ANSWERS)

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))
        node = _make_node({"state": "route me", "preset": "router"})
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        meta = result.data["decision_meta"]
        assert meta["route"] == "laya/decide"
        assert seen["url"].endswith("/v1/laya/decide")
        assert seen["body"]["preset"] == "router" and seen["body"]["prompt"] == "route me"

    async def test_state_truncated_to_900(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=GOOD_ANSWERS)

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))
        node = _make_node({"state": "x" * 2000, "preset": "router"})
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert len(seen["body"]["prompt"]) == 900
        assert result.data["decision_meta"]["state_chars"] == 900


class TestFallback:
    async def test_noul_falls_back_to_llm(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answers": {"escalate": {"type": "noul", "confidence": 0.2}}})

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))

        class _FakeLLM:
            async def chat(self, model, messages, max_tokens=0, **kw):
                assert model == "test-model"
                assert "State:" in messages[0]["content"]
                return type(
                    "R",
                    (),
                    {
                        "content": json.dumps({"answers": {"escalate": {"choice": "continue", "confidence": 0.8}}}),
                        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                    },
                )()

        monkeypatch.setattr("fusion_cowork.ai.mlx_client.FusionMLXClient", lambda: _FakeLLM())
        node = _make_node(
            {
                "state": "review step done",
                "questions": {"escalate": {"type": "choice", "instructions": "e?"}},
                "model": "test-model",
            }
        )
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        meta = result.data["decision_meta"]
        assert meta["engine"] == "llm-fallback"
        assert "noul" in meta["fallback_reason"]
        assert meta["tokens"]["prompt_tokens"] == 120
        assert meta["tokens_saved_vs_llm_turn"] is False
        assert result.data["answers"]["escalate"]["choice"] == "continue"

    async def test_low_confidence_falls_back(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answers": {"go": {"type": "choice", "confidence": 0.31}}})

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))

        class _FakeLLM:
            async def chat(self, model, messages, max_tokens=0, **kw):
                return type(
                    "R",
                    (),
                    {
                        "content": '{"answers": {"go": {"choice": "stop"}}}',
                        "usage": {"prompt_tokens": 50, "completion_tokens": 5},
                    },
                )()

        monkeypatch.setattr("fusion_cowork.ai.mlx_client.FusionMLXClient", lambda: _FakeLLM())
        node = _make_node({"state": "s", "questions": {"go": {"type": "score", "instructions": "g?"}}, "model": "m"})
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert result.data["decision_meta"]["engine"] == "llm-fallback"
        assert "low-confidence" in result.data["decision_meta"]["fallback_reason"]

    async def test_transport_error_falls_back(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))

        class _FakeLLM:
            async def chat(self, model, messages, max_tokens=0, **kw):
                return type(
                    "R",
                    (),
                    {
                        "content": '{"answers": {"a": {"choice": "x"}}}',
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                )()

        monkeypatch.setattr("fusion_cowork.ai.mlx_client.FusionMLXClient", lambda: _FakeLLM())
        node = _make_node({"state": "s", "questions": {"a": {"type": "choice", "instructions": "a?"}}, "model": "m"})
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        meta = result.data["decision_meta"]
        assert meta["engine"] == "llm-fallback" and "decision api error" in meta["fallback_reason"]

    async def test_fallback_disabled_fails_visibly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answers": {"a": {"type": "noul", "confidence": 0.1}}})

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))
        node = _make_node(
            {"state": "s", "questions": {"a": {"type": "choice", "instructions": "a?"}}, "fallback_llm": False}
        )
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED
        assert "fallback disabled" in result.error

    async def test_fallback_without_model_fails_visibly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answers": {"a": {"type": "noul", "confidence": 0.1}}})

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))
        monkeypatch.delenv("FUSION_MLX_MODEL", raising=False)
        node = _make_node({"state": "s", "questions": {"a": {"type": "choice", "instructions": "a?"}}})
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED
        assert "no model set" in result.error

    async def test_llm_fenced_json_parsed(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answers": {"a": {"type": "noul", "confidence": 0.1}}})

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))

        class _FakeLLM:
            async def chat(self, model, messages, max_tokens=0, **kw):
                content = 'Sure!\n```json\n{"answers": {"a": {"choice": "yes", "confidence": 0.9}}}\n```'
                return type("R", (), {"content": content, "usage": {"prompt_tokens": 9, "completion_tokens": 3}})()

        monkeypatch.setattr("fusion_cowork.ai.mlx_client.FusionMLXClient", lambda: _FakeLLM())
        node = _make_node({"state": "s", "questions": {"a": {"type": "choice", "instructions": "a?"}}, "model": "m"})
        result = await node.execute({})
        assert result.data["answers"]["a"]["choice"] == "yes"


class TestValidationAndRegistry:
    async def test_missing_state_fails(self):
        node = _make_node({})
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED and "state" in result.error

    def test_registered_and_aliased(self):
        import_all_nodes()
        assert any(n["name"] == "decision_gate" for n in NodeRegistry.list())
        from fusion_cowork import NODE_NAME_ALIASES

        assert NODE_NAME_ALIASES["决策门"] == "decision_gate"

    def test_decision_url_env_override(self, monkeypatch):
        monkeypatch.setenv("FUSION_MLX_DECISION_URL", "http://127.0.0.1:19999")
        node = _make_node({"state": "s"})
        assert node._decide_url("") == "http://127.0.0.1:19999/v1/decisions"
        assert node._decide_url("router") == "http://127.0.0.1:19999/v1/laya/decide"


class TestWorkflowIntegration:
    async def test_node_runs_inside_dag(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=GOOD_ANSWERS)

        monkeypatch.setattr(decision_gate_node, "_new_client", _client_factory(handler))
        from fusion_cowork.engine import Workflow

        wf = Workflow(name="gate-517")
        node = NodeRegistry.create("decision_gate")
        node.id = "gate"
        node.config.params = {
            "state": {"stage": "verify", "ok": True},
            "questions": {
                "escalate": {
                    "type": "choice",
                    "instructions": "escalate or continue?",
                    "criteria": ["escalate", "continue"],
                }
            },
        }
        wf.add_node(node)
        eng = WorkflowEngine()
        execution = await eng.execute(wf)
        assert execution.status.value == "success"
        step = execution.steps[0]
        assert step.output_data["decision_meta"]["engine"] == "laya"
        assert step.output_data["answers"]["escalate"]["confidence"] == 0.91


class TestValidation:
    async def test_questions_and_preset_required(self):
        node = _make_node({"state": "s"})
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED and "preset" in result.error
