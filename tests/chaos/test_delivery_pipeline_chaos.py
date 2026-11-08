"""issue #386: delivery pipeline chaos pack — one test per historical defect class.

Seven scenarios map to offline, deterministic fault injections. Each test
documents its source issue and asserts the invariant that WOULD HAVE caught
the original defect:

- S1 crash boundary (daemon kill between state change and side effect)
  issues #191/#372/#382 — pause/directive state survives restart; outbox
  receipts dedupe writes across a bridge (process) restart
- S2 pipeline rerun at the same scope issues #352/#368/#382 — same-scope
  rerun dedupes: title-search hit short-circuits create; outbox receipt
  replay skips the duplicate POST
- S3 upstream 503 retry exhaust issues #182 — business failure with
  retry opt-in burns every attempt, keeps attempt_errors, and the run
  ends FAILED (honest), never silently "success"
- S4 caller cancel vs upstream breaker fusion-gateway#577 — cancelling a
  hung chat() must NOT open the circuit breaker (CancelledError is
  BaseException, never recorded as upstream failure); contrast case:
  real 5xx responses do trip it
- S5 unknown placeholder in assembled template issues #343/#389 —
  dangling ${...} refs fail the node loud with unresolved_refs, never a
  cryptic downstream TypeError
- S6 token budget exhaustion mid-run as#646/#373 — Max-iterations error
  triggers a report-only recovery run; the artifact (report) survives
- S7 daemon health version drift fusion-cowork F5/#204 — desk.health
  reports the live package __version__, never a hardcoded literal
"""

import asyncio
import json

import httpx
import pytest

import fusion_cowork
from fusion_cowork.engine.node import BaseNode, NodeRegistry, NodeResult, NodeStatus, register_node
from fusion_cowork.engine.outbox import (
    OutboxBridge,
    get_outbox_bridge,
    outbox_ctx,
    reset_outbox_bridge,
)
from fusion_cowork.engine.run_persistence import RunStateStore
from fusion_cowork.engine.workflow import Workflow, WorkflowEngine, WorkflowStatus
from fusion_cowork.nodes.ops.ops_nodes import GitHubNode

pytestmark = pytest.mark.chaos

pytest.importorskip("fusion_harness")


# ---------------------------------------------------------------- S1


class TestS1CrashBoundary:
    async def test_paused_state_survives_daemon_restart(self, tmp_path):
        # issues #191/#372: kill -9 between pause and persist must not lose control state.
        # Fault injected: "restart" = a brand-new RunStateStore over the same db file.
        db = tmp_path / "run_states.db"
        store = RunStateStore(db)
        store.save(
            {
                "run_id": "run-s1",
                "workflow_id": "wf",
                "workflow_name": "wf",
                "status": "paused",
                "steps": [{"node_id": "n1", "status": "success"}],
                "directives": [{"type": "insert_node", "node": {"id": "nx"}}],
                "memory": True,
            },
            paused_reason="hitl_pending",
        )
        # daemon restart
        store2 = RunStateStore(db)
        rows = store2.load_active()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "hitl_pending"
        assert json.loads(row["directives"])[0]["type"] == "insert_node"
        assert json.loads(row["steps"])[0]["node_id"] == "n1"

    async def test_outbox_receipt_dedupes_write_across_bridge_restart(self, monkeypatch, tmp_path):
        # issue #382: crash after write, before receipt... the narrowed window:
        # a REPLAYED (crash-free) restart must not re-run a receipted command.
        db = str(tmp_path / "outbox.db")
        monkeypatch.setenv("FUSION_COWORK_OUTBOX_DB", db)
        reset_outbox_bridge()
        try:
            calls = []

            async def _write():
                calls.append(1)
                return {"number": 1}

            bridge = get_outbox_bridge()
            assert bridge is not None and bridge.enabled
            outbox_ctx.set({"run_id": "run-s1", "node_id": "n1", "node_name": "gh"})
            data, replayed = await bridge.execute_idempotent_async("run-s1:n1:issue_create:t", "github:o/r", _write)
            assert replayed is False and data == {"number": 1}

            # process "restart": new bridge instance over the same db
            bridge2 = OutboxBridge(db)
            data2, replayed2 = await bridge2.execute_idempotent_async("run-s1:n1:issue_create:t", "github:o/r", _write)
            assert replayed2 is True
            assert data2 == {"number": 1}
            assert len(calls) == 1  # underlying effect ran exactly once
        finally:
            reset_outbox_bridge()
            outbox_ctx.set(None)


# ---------------------------------------------------------------- S2


class TestS2RerunSameScope:
    def _patch_github(self, monkeypatch, posts):
        monkeypatch.setenv("GITHUB_TOKEN", "t386")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/search/issues":
                return httpx.Response(200, json={"items": []})
            if request.method == "POST" and request.url.path == "/repos/o/r/issues":
                posts.append(1)
                return httpx.Response(201, json={"number": 7, "title": "t386", "html_url": "u"})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        monkeypatch.setattr(
            GitHubNode,
            "_client",
            lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
        )

    def _node(self):
        node = GitHubNode.__new__(GitHubNode)
        return node

    async def test_title_search_dedupes_second_run(self, monkeypatch):
        # issues #352/#368: rerun at same scope — second create with same title
        # reuses the existing issue (search hit, all states), never POSTs again.
        posts: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/search/issues":
                if posts:
                    return httpx.Response(
                        200,
                        json={"items": [{"number": 7, "title": "t386", "state": "closed", "html_url": "u"}]},
                    )
                return httpx.Response(200, json={"items": []})
            if request.method == "POST" and request.url.path == "/repos/o/r/issues":
                posts.append(1)
                return httpx.Response(201, json={"number": 7, "title": "t386", "html_url": "u"})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        monkeypatch.setenv("GITHUB_TOKEN", "t386")
        monkeypatch.setattr(
            GitHubNode,
            "_client",
            lambda self, b, t, to: httpx.AsyncClient(base_url=b, transport=httpx.MockTransport(handler), timeout=to),
        )
        params = {"op": "issue_create", "repo": "o/r", "title": "t386", "body": "b", "allowed_repos": ["o/r"]}
        node = GitHubNode(node_id="g1", config=type("C", (), {"params": params})())
        r1 = await node.execute({})
        assert r1.status == NodeStatus.SUCCESS
        assert len(posts) == 1
        r2 = await node.execute({})  # rerun, same scope
        assert r2.status == NodeStatus.SUCCESS
        assert r2.data["deduplicated"] is True
        assert len(posts) == 1  # no duplicate write

    async def test_outbox_receipt_dedupes_replayed_run_node(self, outbox_env_386, monkeypatch):
        # issue #382: same run+node+op replay (resume/rerun inside the receipt
        # window) skips the POST entirely.
        posts: list = []
        self._patch_github(monkeypatch, posts)
        bridge = get_outbox_bridge()
        assert bridge is not None
        outbox_ctx.set({"run_id": "run-s2", "node_id": "g1", "node_name": "github_api"})
        try:
            params = {"op": "issue_create", "repo": "o/r", "title": "t386", "body": "b", "allowed_repos": ["o/r"]}
            node = GitHubNode(node_id="g1", config=type("C", (), {"params": params})())
            r1 = await node.execute({})
            assert r1.status == NodeStatus.SUCCESS and len(posts) == 1
            r2 = await node.execute({})  # replayed command_id
            assert r2.status == NodeStatus.SUCCESS
            assert r2.data.get("deduplicated") is not True or len(posts) == 1
            assert len(posts) == 1  # receipt replay skipped the write
        finally:
            outbox_ctx.set(None)


@pytest.fixture()
def outbox_env_386(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_COWORK_OUTBOX_DB", str(tmp_path / "outbox2.db"))
    monkeypatch.delenv("FUSION_COWORK_OUTBOX", raising=False)
    reset_outbox_bridge()
    yield
    reset_outbox_bridge()


# ---------------------------------------------------------------- S3


class _FlakyFailNode(BaseNode):
    name = "chaos_flaky_fail_386"
    display_name = "Chaos Flaky Fail"
    category = "tool"
    description = "always returns FAILED"
    icon = "x"
    default_label = "flaky"
    counter: list = []

    async def execute(self, inputs):
        type(self).counter.append(1)
        return NodeResult(status=NodeStatus.FAILED, error="upstream 503: degraded")


class TestS3RetryExhaustHonestFailure:
    async def test_business_retry_burns_attempts_then_run_fails(self):
        # issue #182: retry_on_business_failure opt-in retries FAILED results,
        # but exhaustion must leave the run FAILED with every attempt recorded.
        register_node(_FlakyFailNode)
        try:
            _FlakyFailNode.counter = []
            wf = Workflow.from_dict(
                {
                    "id": "wf-s3",
                    "name": "s3",
                    "nodes": [
                        {
                            "id": "n1",
                            "name": "chaos_flaky_fail_386",
                            "config": {
                                "params": {},
                                "max_retries": 2,
                                "retry_delay": 0,
                                "retry_on_business_failure": True,
                            },
                        }
                    ],
                    "edges": [],
                }
            )
            engine = WorkflowEngine()
            execution = await engine.execute(wf)
            assert execution.status == WorkflowStatus.FAILED
            assert len(_FlakyFailNode.counter) == 3  # 1 initial + 2 retries, all burned
            step = next(s for s in execution.steps if s.node_id == "n1")
            assert step.status == NodeStatus.FAILED
            assert len(step.attempt_errors) == 3  # every attempt recorded honestly
            assert "503" in step.attempt_errors[0]
        finally:
            NodeRegistry.unregister("chaos_flaky_fail_386", force=True)


# ---------------------------------------------------------------- S4


class TestS4CancelNeverOpensBreaker:
    def _client(self, handler, monkeypatch=None):
        import fusion_cowork.ai.mlx_client as mlx_mod
        from fusion_cowork.ai.mlx_client import FusionMLXClient

        if monkeypatch is not None:
            # force the in-module chat path (no fusion-core with_retry) so the
            # injected transport is the one actually exercised
            monkeypatch.setattr(mlx_mod, "_HAS_FUSION_CORE", False)
        c = FusionMLXClient(base_url="http://cb-test", api_key="k", max_retries=0)
        c._client = httpx.AsyncClient(base_url="http://cb-test", transport=httpx.MockTransport(handler))
        return c

    async def test_cancelled_chat_leaves_breaker_closed(self, monkeypatch):
        # fusion-gateway#577: caller cancels a hung upstream call. CancelledError
        # is BaseException — it must propagate WITHOUT recording a breaker
        # failure (the upstream is not at fault; a cancel must never open the
        # circuit and lock out every later call).
        from fusion_cowork.security.circuit_breaker import CircuitBreaker, CircuitState

        async def hang(request):
            await asyncio.sleep(3600)

        c = self._client(hang, monkeypatch)

        c = self._client(hang)
        c._breaker = CircuitBreaker(name="mlx", failure_threshold=2, recovery_timeout=30)
        task = asyncio.create_task(c.chat(messages=[{"role": "user", "content": "hi"}], model="m"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert c._breaker.state == CircuitState.CLOSED
        assert c._breaker.is_call_allowed()  # later calls still go through

    async def test_upstream_unreachable_still_trips_breaker(self, monkeypatch):
        # contrast: genuine upstream failures (connect-level, retried to
        # exhaustion) DO trip the breaker (Stage 6). Cancel must never land
        # in this path — that is the S4 primary assertion.
        from fusion_cowork.security.circuit_breaker import CircuitBreaker, CircuitState

        def handler(request):
            raise httpx.ConnectError("conn refused", request=request)

        c = self._client(handler, monkeypatch)
        c._breaker = CircuitBreaker(name="mlx", failure_threshold=2, recovery_timeout=30)
        for _ in range(2):
            with pytest.raises(httpx.ConnectError):
                await c.chat(messages=[{"role": "user", "content": "hi"}], model="m")
        assert c._breaker.is_call_allowed() is False
        assert c._breaker.state in (CircuitState.OPEN, CircuitState.HALF_OPEN)


# ---------------------------------------------------------------- S5


class _EchoNode(BaseNode):
    name = "chaos_echo_386"
    display_name = "Chaos Echo"
    category = "tool"
    description = "echo"
    icon = "x"
    default_label = "echo"

    async def execute(self, inputs):
        return NodeResult(status=NodeStatus.SUCCESS, data={"ok": True}, summary="echo")


class TestS5UnknownPlaceholderFailsLoud:
    async def test_dangling_step_ref_fails_node_with_unresolved_refs(self):
        # issues #343/#389: template assembled against a renamed node id leaves
        # a dangling ${step.<id>.<path>} — the node must FAIL LOUD naming the
        # ref, never hand a literal "${...}" string to int()/float() downstream.
        register_node(_EchoNode)
        try:
            wf = Workflow.from_dict(
                {
                    "id": "wf-s5",
                    "name": "s5",
                    "nodes": [
                        {"id": "n1", "name": "chaos_echo_386", "config": {"params": {}}},
                        {"id": "n2", "name": "chaos_echo_386", "config": {"params": {"url": "${step.ghost.pr_url}"}}},
                    ],
                    "edges": [{"source_id": "n1", "target_id": "n2"}],
                }
            )
            engine = WorkflowEngine()
            execution = await engine.execute(wf)
            assert execution.status == WorkflowStatus.FAILED
            step = next(s for s in execution.steps if s.node_id == "n2")
            assert step.status == NodeStatus.FAILED
            assert "unresolved interpolation" in (step.error or execution.error or "")
            assert "step.ghost.pr_url" in str(step.output_data.get("unresolved_refs"))
        finally:
            NodeRegistry.unregister("chaos_echo_386", force=True)


# ---------------------------------------------------------------- S6


class TestS6BudgetExhaustionReportRecovery:
    async def test_max_iterations_error_recovers_with_report_only_run(self, monkeypatch):
        # as#646/#373: studio_run sees the Max-iterations budget error and
        # recovers via a report-only follow-up run — the artifact survives.
        from tests.test_issue_373_report_recovery import (
            _MAX_ITER_ERROR,
            _POSTS,
            _node,
            _patch_transport,
            _runs_handler,
        )

        _POSTS.clear()
        handler = _runs_handler(
            _POSTS,
            first_result={"last_error": _MAX_ITER_ERROR, "artifacts": []},
            second_result={"report": "chaos recovery report", "artifacts": ["a1"]},
        )
        _patch_transport(monkeypatch, handler)
        res = await _node(max_turns=24, idempotency_key="k-386").execute({})
        assert res.status.value == "success"
        assert res.data["report_recovery"] is True
        assert res.data["run_id"] == "r2"
        assert len(_POSTS) == 2
        assert "REPORT ONLY" in _POSTS[1]["goal"]


# ---------------------------------------------------------------- S7


class TestS7HealthVersionTruth:
    async def test_desk_health_reports_live_package_version(self):
        # F5/#204: desk.health version must equal the live package
        # __version__ — a hardcoded literal silently rots on every bump.
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        srv = DeskRPCServer.__new__(DeskRPCServer)
        srv._space_store = None
        result = await srv._handle_health({})
        assert result["service"] == "fusion-cowork"
        assert result["version"] == fusion_cowork.__version__
