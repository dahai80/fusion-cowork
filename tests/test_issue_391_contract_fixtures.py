"""issue #391: studio_run consumer-side contract tests against shared fixtures.

Fixtures are the single source of truth and live in fusion-agent-studio
tests/contract/fixtures/ (agent-studio#652 producer pack, spec
verification-infra-spec-1108.md §3.1). Consumption mode: VENDORED COPY
(tests/contract/fixtures/) + sync check — the sync test below compares
byte-identical against the sibling checkout
../fusion-agent-studio/tests/contract/fixtures/ when it exists (skipped in
CI, where only this repo is checked out); refresh with:

    for f in runs_submit_response run_status_response run_result_response graph_execute_request; do \\
      gh api repos/dahai80/fusion-agent-studio/contents/tests/contract/fixtures/$f.json \\
        --jq .content | base64 -d > tests/contract/fixtures/$f.json; done

Cowork reads asserted per fixture:
- runs_submit_response.json  -> run_id / status
- run_status_response.json   -> terminal polling (completed/failed/canceled)
- run_result_response.json   -> last_error / artifacts / last_result + budget marker
- graph_execute_request.json -> body carries graph_id (cow#135 F-1)
"""

import json
from pathlib import Path

import httpx
import pytest

from fusion_cowork.nodes.ecosystem.studio_node import (
    StudioRunNode,
    _is_budget_exhausted,
)

FIXTURES = Path(__file__).resolve().parent / "contract" / "fixtures"
UPSTREAM_SIBLING = (
    Path(__file__).resolve().parent.parent.parent / "fusion-agent-studio" / "tests" / "contract" / "fixtures"
)

VENDORED = [
    "runs_submit_response.json",
    "run_status_response.json",
    "run_result_response.json",
    "graph_execute_request.json",
]


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _node(**extra):
    params = {"input": "deliver the thing", "poll_interval": 0.5, "mode": "sync", **extra}
    return StudioRunNode(node_id="s391", config=type("C", (), {"params": params})())


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestVendoredFixtures:
    @pytest.mark.parametrize("name", VENDORED)
    def test_fixture_parses_with_contract_banner(self, name):
        data = _fixture(name)
        assert "_contract" in data, f"{name} missing _contract banner"

    @pytest.mark.parametrize("name", VENDORED)
    def test_sync_with_upstream_sibling(self, name):
        # vendored copy must stay byte-identical with the producer pack;
        # skipped when fusion-agent-studio is not checked out alongside (CI)
        if not UPSTREAM_SIBLING.exists():
            pytest.skip("fusion-agent-studio sibling checkout not present")
        assert (FIXTURES / name).read_bytes() == (UPSTREAM_SIBLING / name).read_bytes(), (
            f"{name} drifted from fusion-agent-studio fixture — re-run the refresh loop in the module docstring"
        )


class TestRunsSubmitResponse:
    async def test_async_submit_reads_run_id_and_status(self, monkeypatch):
        # runs_submit_response.json — cowork reads run_id + status (spec_applied
        # and effective_overrides are upstream-only fields, ignored here)
        fixture = _fixture("runs_submit_response.json")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST" and request.url.path == "/v1/runs"
            return httpx.Response(200, json=fixture)

        _patch_transport(monkeypatch, handler)
        res = await _node(agent_id="delivery-dev", mode="async").execute({})
        assert res.status.value == "success"
        assert res.data["run_id"] == fixture["run_id"]
        assert res.data["status"] == fixture["status"]


class TestRunStatusResponse:
    async def test_terminal_polling_completed(self, monkeypatch):
        # run_status_response.json — poll GET /v1/runs/{id} until terminal
        # (completed/failed/canceled), then fetch /result
        fixture = _fixture("run_status_response.json")
        result = _fixture("run_result_response.json")

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/v1/runs":
                return httpx.Response(200, json=_fixture("runs_submit_response.json"))
            if request.method == "GET" and path == "/v1/runs/task-1731000000-1":
                return httpx.Response(200, json=fixture)
            if request.method == "GET" and path == "/v1/runs/task-1731000000-1/result":
                return httpx.Response(200, json=result)
            raise AssertionError(f"unexpected {request.method} {path}")

        _patch_transport(monkeypatch, handler)
        res = await _node(agent_id="delivery-dev").execute({})
        assert res.status.value == "success"
        assert res.data["status"] == "completed"
        assert res.data["run_id"] == fixture["run_id"]
        assert res.data["result"]["artifacts"] == result["artifacts"]

    @pytest.mark.parametrize("terminal", ["completed", "failed", "canceled"])
    async def test_terminal_statuses_recognized(self, monkeypatch, terminal):
        # every contract-declared terminal status ends the poll loop (no hang)
        run_status = dict(_fixture("run_status_response.json"))
        run_status["status"] = terminal

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/v1/runs":
                return httpx.Response(200, json=_fixture("runs_submit_response.json"))
            if request.method == "GET" and path.endswith("/result"):
                return httpx.Response(200, json=_fixture("run_result_response.json"))
            if request.method == "GET" and path.startswith("/v1/runs/"):
                return httpx.Response(200, json=run_status)
            raise AssertionError(f"unexpected {request.method} {path}")

        _patch_transport(monkeypatch, handler)
        res = await _node(agent_id="delivery-dev", report_recovery=False).execute({})
        if terminal == "completed":
            assert res.status.value == "success"
        else:
            assert res.status.value == "failed"
            assert terminal in res.error


class TestRunResultResponse:
    async def test_result_reads_and_budget_marker(self, monkeypatch):
        # run_result_response.json — last_error / artifacts / last_result are
        # the cowork read surface; the budget-exhaustion marker lives in last_error
        result = dict(_fixture("run_result_response.json"))
        result["status"] = "failed"
        result["last_error"] = "Max iterations exceeded after 24/24 turns (249019 tokens, report_len=0)"
        run_status = dict(_fixture("run_status_response.json"))
        run_status["status"] = "failed"

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/v1/runs":
                return httpx.Response(200, json=_fixture("runs_submit_response.json"))
            if request.method == "GET" and path == "/v1/runs/task-1731000000-1":
                return httpx.Response(200, json=run_status)
            if request.method == "GET" and path == "/v1/runs/task-1731000000-1/result":
                return httpx.Response(200, json=result)
            raise AssertionError(f"unexpected {request.method} {path}")

        _patch_transport(monkeypatch, handler)
        res = await _node(agent_id="delivery-dev", report_recovery=False).execute({})
        assert res.status.value == "failed"
        assert "Max iterations" in res.error
        assert res.data["result"]["artifacts"] == result["artifacts"]
        assert res.data["result"]["last_result"]["report"] == "# Delivery Report"
        assert _is_budget_exhausted(res) is True


class TestGraphExecuteRequest:
    async def test_body_carries_graph_id(self, monkeypatch):
        # graph_execute_request.json — body MUST carry graph_id even though it
        # is redundant with the path (cow#135 F-1: missing -> upstream 422)
        fixture = _fixture("graph_execute_request.json")
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST" and request.url.path == "/v1/graphs/graph-1/execute"
            seen.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"status": "completed", "events": [{"type": "node_end", "node_id": "n1"}]},
            )

        _patch_transport(monkeypatch, handler)
        res = await _node(graph_id="graph-1", mode="sync").execute({})
        assert res.status.value == "success"
        # body matches the contract fixture exactly
        assert seen == {"graph_id": fixture["graph_id"], "input_text": fixture["input_text"]}
