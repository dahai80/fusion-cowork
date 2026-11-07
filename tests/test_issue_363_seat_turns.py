"""issue #363: studio_run 座位轮次预算 — 显式 spec.max_turns 传入 /v1/runs。

studio 默认 8 轮会饿死多产物角色 (env-mock/architect, live run_ddeaa4d693e9
"Max iterations exceeded" turns=8); product_lifecycle 模板暴露 seat_max_turns
(缺省 24) 透传三个 studio_run 座位节点。
"""

import json

import httpx

from fusion_cowork.nodes.ecosystem.studio_node import StudioRunNode
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES, TemplateManager


def _tpl():
    return next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")


def _node(**extra):
    params = {"input": "write doc", "agent_id": "a1", "mode": "async", **extra}
    return StudioRunNode(node_id="s1", config=type("C", (), {"params": params})())


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestStudioNodeMaxTurns:
    async def test_max_turns_sent_as_spec(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"run_id": "r1", "status": "pending"})

        _patch_transport(monkeypatch, handler)
        res = await _node(max_turns=24).execute({})
        assert res.status.value == "success", res.error
        assert seen["body"]["spec"] == {"max_turns": 24}

    async def test_no_max_turns_omits_spec(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"run_id": "r1", "status": "pending"})

        _patch_transport(monkeypatch, handler)
        res = await _node().execute({})
        assert res.status.value == "success", res.error
        assert "spec" not in seen["body"]

    def test_schema_declares_max_turns(self):
        assert "max_turns" in _node().get_params_schema()["properties"]


class TestTemplateSeatBudget:
    def test_seat_max_turns_param_default(self):
        assert _tpl()["params"]["properties"]["seat_max_turns"]["default"] >= 24

    def test_all_three_seats_receive_max_turns(self):
        for nid in ("n1", "n3", "n9"):
            node = next(n for n in _tpl()["workflow"]["nodes"] if n["id"] == nid)
            assert node["config"]["params"]["max_turns"] == "${param.seat_max_turns}", nid

    def test_substitution_preserves_int(self):
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle", param_values={"slug": "demo", "repo": "o/x", "task_title": "t"}
        )
        for nid in ("n1", "n3", "n9"):
            assert wf.nodes[nid].config.params["max_turns"] >= 24, nid
