"""issue #373: env-mock 座位轮次耗尽但交付物已落盘 — report-only 续跑回收。

- seat_max_turns 缺省 24 → 48 (live 证据 run_e5d4d34da020: 24/24 耗尽 report_len=0)
- studio_run 终态 failed 且 last_error 含 "Max iterations" → 自动补一轮
  report-only 续跑 (不重做工作, 只写最终报告), 成功则 SUCCESS + report_recovery
- 非轮次耗尽失败 / recovery 关闭 / 续跑仍失败 → 原失败原样上抛
"""

import json

import httpx

from fusion_cowork.nodes.ecosystem.studio_node import StudioRunNode
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES, TemplateManager


def _tpl():
    return next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")


def _node(**extra):
    params = {"input": "write doc", "agent_id": "a1", "mode": "sync", **extra}
    return StudioRunNode(node_id="s1", config=type("C", (), {"params": params})())


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


_MAX_ITER_ERROR = "Max iterations exceeded after 24/24 turns (249019 tokens, report_len=0) — budget exhausted mid-work"

_POSTS = []


def _runs_handler(seen_posts, first_status="failed", first_result=None, second_status="completed", second_result=None):
    """Two-run fake upstream: first POST → r1 (terminal first_status), second POST → r2."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/runs":
            _POSTS.append(json.loads(request.content))
            run_id = f"r{len(_POSTS)}"
            return httpx.Response(200, json={"run_id": run_id, "status": "pending"})
        if request.method == "GET" and path in ("/v1/runs/r1", "/v1/runs/r2"):
            status = first_status if path.endswith("r1") else second_status
            return httpx.Response(200, json={"run_id": path.rsplit("/", 1)[-1], "status": status})
        if request.method == "GET" and path in ("/v1/runs/r1/result", "/v1/runs/r2/result"):
            result = first_result if path.endswith("r1/result") else second_result
            return httpx.Response(200, json=result or {})
        raise AssertionError(f"unexpected {request.method} {path}")

    return handler


class TestReportRecovery:
    async def test_budget_exhausted_recovers_with_report_only_run(self, monkeypatch):
        _POSTS.clear()
        handler = _runs_handler(
            _POSTS,
            first_result={"last_error": _MAX_ITER_ERROR, "artifacts": []},
            second_result={"report": "final report text", "artifacts": ["a1"]},
        )
        _patch_transport(monkeypatch, handler)
        res = await _node(max_turns=24, idempotency_key="key-1").execute({})
        assert res.status.value == "success", res.error
        assert res.data["report_recovery"] is True
        assert res.data["recovered_from"] == "r1"
        assert res.data["run_id"] == "r2"
        assert "report-only recovery run" in res.summary
        # 两次提交 — 续跑 goal 为 report-only 指令, 且不带原 run 幂等键
        assert len(_POSTS) == 2
        assert "REPORT ONLY" in _POSTS[1]["goal"]
        assert "r1" in _POSTS[1]["goal"]
        assert "idempotency_key" not in _POSTS[1]
        assert _POSTS[1].get("spec") == {"max_turns": 24}

    async def test_non_budget_failure_no_recovery(self, monkeypatch):
        _POSTS.clear()
        handler = _runs_handler(_POSTS, first_result={"last_error": "boom: sandbox denied"})
        _patch_transport(monkeypatch, handler)
        res = await _node().execute({})
        assert res.status.value == "failed"
        assert "boom" in res.error
        assert len(_POSTS) == 1

    async def test_recovery_disabled_fails_through(self, monkeypatch):
        _POSTS.clear()
        handler = _runs_handler(_POSTS, first_result={"last_error": _MAX_ITER_ERROR})
        _patch_transport(monkeypatch, handler)
        res = await _node(report_recovery=False).execute({})
        assert res.status.value == "failed"
        assert "Max iterations" in res.error
        assert len(_POSTS) == 1

    async def test_recovery_run_failure_surfaces_original(self, monkeypatch):
        _POSTS.clear()
        handler = _runs_handler(
            _POSTS,
            first_result={"last_error": _MAX_ITER_ERROR},
            second_status="failed",
            second_result={"last_error": "still stuck"},
        )
        _patch_transport(monkeypatch, handler)
        res = await _node().execute({})
        assert res.status.value == "failed"
        assert "Max iterations" in res.error
        assert len(_POSTS) == 2

    async def test_completed_run_no_recovery(self, monkeypatch):
        _POSTS.clear()
        handler = _runs_handler(_POSTS, first_status="completed", first_result={"report": "done", "artifacts": ["a1"]})
        _patch_transport(monkeypatch, handler)
        res = await _node().execute({})
        assert res.status.value == "success", res.error
        assert "report_recovery" not in res.data
        assert len(_POSTS) == 1

    def test_schema_declares_report_recovery(self):
        assert "report_recovery" in _node().get_params_schema()["properties"]


class TestTemplateSeatBudget48:
    def test_seat_max_turns_param_default_48(self):
        assert _tpl()["params"]["properties"]["seat_max_turns"]["default"] == 48

    def test_substitution_yields_48(self):
        wf = TemplateManager().template_to_workflow(
            "product_lifecycle", param_values={"slug": "demo", "repo": "o/x", "task_title": "t"}
        )
        for nid in ("n1", "n3", "n9"):
            assert wf.nodes[nid].config.params["max_turns"] == 48, nid
