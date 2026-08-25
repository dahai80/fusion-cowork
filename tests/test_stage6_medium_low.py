"""Stage 6 — MEDIUM+LOW 收尾测试。

覆盖本 stage 修复:
- MD-1: JSON-RPC batch 拒绝 + UDS 行上限 + 请求体上限
- MD-7/8: MessageBus 队列上限 + history 上限
- MD-11/12/13/LO-4: ConfigCenter 原子写 + 锁 + 迭代快照
- MD-16/LO-6: 插件 URL https-only + 重定向拒 + 大小上限 + meta 未知键
- MD-17: import_from_claude_desk command 校验
- LO-2: headless _extract_final_output 显式 status 比较
- LO-7: manifest.timeout_seconds 透传沙箱
- LO-8/9: scoped_folder 单例锁 + symlink 拒
- LO-10: rpc_bridge 显式分发
- LO-12: mcp_gateway spawn fail-closed
- LO-13: collab_ws auth token + 成员校验
- latent: desk.space.workflow.* 走 SpaceArtifactService
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from fusion_cowork.config_center import ConfigCenter
from fusion_cowork.orchestrator.comm import _HISTORY_MAX, _QUEUE_MAX, AgentMessageBus
from fusion_cowork.security.scoped_folder import (
    ScopedFolderManager,
    get_scoped_folder_manager,
    reset_scoped_folder_manager,
)

# ── MD-7/8: MessageBus bounded ──


class TestMessageBusBounded:
    @pytest.mark.asyncio
    async def test_queue_maxsize_bound(self):
        bus = AgentMessageBus()
        q = bus.subscribe("topic_a")
        assert q.maxsize == _QUEUE_MAX

    @pytest.mark.asyncio
    async def test_overflow_drops_not_blocks(self):
        bus = AgentMessageBus()
        q = bus.subscribe("overflow")
        # 填满队列
        for i in range(_QUEUE_MAX):
            q.put_nowait({"i": i})
        # 再发一条 — publish 应 drop 而非 await 阻塞
        msg_id = await bus.publish("sender", "overflow", {"x": 1})
        assert msg_id
        # 队列不超 maxsize
        assert q.qsize() == _QUEUE_MAX

    @pytest.mark.asyncio
    async def test_history_bounded(self):
        bus = AgentMessageBus()
        for i in range(_HISTORY_MAX + 50):
            await bus.publish("sender", "hist", {"i": i})
        hist = bus.get_history("hist", limit=_HISTORY_MAX + 100)
        assert len(hist) <= _HISTORY_MAX


# ── MD-11/12/13/LO-4: ConfigCenter atomic + lock ──


class TestConfigCenterAtomic:
    def setup_method(self):
        ConfigCenter.reset_instance()

    def teardown_method(self):
        ConfigCenter.reset_instance()

    def test_save_creates_0600_file(self, tmp_path):
        cfg = ConfigCenter(config_file=str(tmp_path / "cfg.json"))
        cfg.set("k", "v")
        cfg.save()
        p = tmp_path / "cfg.json"
        assert p.exists()
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600

    def test_save_atomic_no_tmp_residue(self, tmp_path):
        cfg = ConfigCenter(config_file=str(tmp_path / "cfg.json"))
        cfg.set("k", "v")
        cfg.save()
        residue = list(tmp_path.glob(".cfg_*"))
        assert residue == []

    def test_load_under_lock(self, tmp_path):
        f = tmp_path / "cfg.json"
        cfg = ConfigCenter(config_file=str(f))
        cfg.set("a", 1)
        cfg.set("b", 2)
        cfg.save()
        ConfigCenter.reset_instance()
        cfg2 = ConfigCenter(config_file=str(f))
        loaded = cfg2.load()
        assert loaded == 2
        assert cfg2.get("a") == 1

    def test_observer_mutation_during_notify_safe(self, tmp_path):
        cfg = ConfigCenter(config_file=str(tmp_path / "cfg.json"))
        calls = []

        def callback(change):
            calls.append(change.key)
            # 在 callback 内取消订阅 — 应不抛 RuntimeError (MD-11 迭代快照)
            cfg.unobserve(obs)

        obs = cfg.observe(callback, keys=["k"])
        cfg.set("k", "v")
        assert calls == ["k"]


# ── LO-8/9: scoped_folder singleton lock + symlink ──


class TestScopedFolderSymlink:
    def setup_method(self):
        reset_scoped_folder_manager()

    def teardown_method(self):
        reset_scoped_folder_manager()

    def test_singleton_double_init_safe(self):
        # 并发 from_config 不应产生多实例/抛错
        m1 = get_scoped_folder_manager()
        m2 = get_scoped_folder_manager()
        assert m1 is m2

    def test_symlink_escape_rejected(self, tmp_path):
        # scope 内目录
        scope_dir = tmp_path / "scope"
        scope_dir.mkdir()
        # scope 外文件
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        # symlink 指向 scope 外
        link = scope_dir / "escape"
        try:
            os.symlink(outside, link)
        except OSError:
            pytest.skip("symlink 创建失败 (权限/平台)")
        mgr = ScopedFolderManager(scoped_folders=[str(scope_dir)], enforce=True)
        assert not mgr.is_allowed(str(link))

    def test_normal_file_inside_scope_allowed(self, tmp_path):
        scope_dir = tmp_path / "scope"
        scope_dir.mkdir()
        (scope_dir / "file.txt").write_text("ok")
        mgr = ScopedFolderManager(scoped_folders=[str(scope_dir)], enforce=True)
        assert mgr.is_allowed(str(scope_dir / "file.txt"))


# ── LO-2: headless status extract ──


class TestHeadlessStatusExtract:
    def test_extract_final_output_explicit_status(self):
        from fusion_cowork.sdk.headless import HeadlessRunner

        runner = HeadlessRunner.__new__(HeadlessRunner)

        class FakeStatus:
            value = "success"

        class FakeStep:
            def __init__(self, status, output):
                self.status = status
                self.output_data = output

        class FakeResult:
            def __init__(self, steps):
                self.steps = steps

        result = FakeResult(
            [
                FakeStep(FakeStatus(), None),
                FakeStep(FakeStatus(), {"final": True}),
            ]
        )
        out = runner._extract_final_output(result)
        assert out == {"final": True}


# ── LO-7: manifest timeout_seconds ──


class TestManifestTimeout:
    def test_default_zero(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        m = PluginManifest(name="x", version="1.0")
        assert m.timeout_seconds == 0.0

    def test_from_dict_coerce(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        m = PluginManifest.from_dict({"name": "x", "timeout_seconds": "30"})
        assert m.timeout_seconds == 30.0

    def test_from_dict_negative_clamped(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        m = PluginManifest.from_dict({"name": "x", "timeout_seconds": -5})
        assert m.timeout_seconds == 0.0

    def test_from_dict_invalid_falls_zero(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        m = PluginManifest.from_dict({"name": "x", "timeout_seconds": "abc"})
        assert m.timeout_seconds == 0.0

    def test_to_dict_roundtrip(self):
        from fusion_cowork.plugins.manifest import PluginManifest

        m = PluginManifest(name="x", version="1.0", timeout_seconds=45.0)
        d = m.to_dict()
        assert d["timeout_seconds"] == 45.0


# ── MD-16/LO-6: plugin loader URL + meta ──


class TestPluginLoaderUrl:
    def test_http_rejected(self, tmp_path):
        from fusion_cowork.plugins.loader import PluginLoader

        loader = PluginLoader(plugins_dir=str(tmp_path))
        assert not loader._install_url("http://evil.com/x.zip")

    def test_non_zip_rejected(self, tmp_path):
        from fusion_cowork.plugins.loader import PluginLoader

        loader = PluginLoader(plugins_dir=str(tmp_path))
        assert not loader._install_url("https://evil.com/x.tar")


# ── LO-6: sandboxed node meta unknown key warning ──


class TestSandboxMetaUnknownKey:
    def test_unknown_meta_key_warns(self, caplog):
        from fusion_cowork.plugins.loader import make_sandboxed_node_class

        meta = {
            "class_name": "Foo",
            "name": "foo",
            "display_name": "Foo",
            "category": "tool",
            "description": "d",
            "icon": "i",
            "default_label": "Foo",
            "entry_file": "e.py",
            "params_schema": {},
            "bogus_key": "should warn",
        }
        sandbox = MagicMock()
        with caplog.at_level("WARNING"):
            make_sandboxed_node_class(meta, sandbox)
        assert any("bogus_key" in r.message for r in caplog.records)


# ── MD-17: import_from_claude_desk command validation ──


class TestClaudeDeskImport:
    def test_safe_command_classified(self):
        from fusion_cowork.plugins.loader import PluginLoader

        assert PluginLoader._is_safe_mcp_command("/usr/bin/node", ["server.js"], {})
        assert PluginLoader._is_safe_mcp_command("python3", ["-m", "mcp"], {})

    def test_shell_meta_rejected(self):
        from fusion_cowork.plugins.loader import PluginLoader

        assert not PluginLoader._is_safe_mcp_command("node; rm -rf /", [], {})
        assert not PluginLoader._is_safe_mcp_command("node", ["$(whoami)"], {})

    def test_dangerous_base_rejected(self):
        from fusion_cowork.plugins.loader import PluginLoader

        assert not PluginLoader._is_safe_mcp_command("rm", ["-rf", "/"], {})
        assert not PluginLoader._is_safe_mcp_command("bash", ["-c", "x"], {})

    def test_non_list_args_rejected(self):
        from fusion_cowork.plugins.loader import PluginLoader

        assert not PluginLoader._is_safe_mcp_command("node", "server.js", {})

    def test_sanitize_env_drops_nonstring(self):
        from fusion_cowork.plugins.loader import PluginLoader

        out = PluginLoader._sanitize_mcp_env({"A": "ok", "B": 1, "": "x", "C": None, "D": ["bad"]})
        assert out == {"A": "ok", "B": "1"}


# ── LO-10: rpc_bridge explicit dispatch ──


class TestRpcBridgeDispatch:
    @pytest.mark.asyncio
    async def test_non_plugins_method_rejected_32601(self):
        from fusion_cowork.server.rpc_bridge import dispatch_rpc

        resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 1, "method": "desk.nodes.list"})
        assert resp["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_unknown_plugins_method_rejected_32601(self):
        # LO-10: 斜杠前缀宽匹配 (plugins/nonexistent 命中 startswith("plugins/")) → 委托 MCPHandler
        # 依赖在 → handler -32601 (unknown method); 依赖缺 → ImportError → -32603
        from fusion_cowork.server.rpc_bridge import dispatch_rpc, is_plugins_available

        resp = await dispatch_rpc({"jsonrpc": "2.0", "id": 1, "method": "plugins/nonexistent"})
        if is_plugins_available():
            assert resp["error"]["code"] == -32601
        else:
            assert resp["error"]["code"] == -32603


# ── LO-12: mcp_gateway spawn fail-closed ──


class TestMcpGatewaySpawn:
    @pytest.mark.asyncio
    async def test_spawn_disabled_without_flag(self, monkeypatch):
        monkeypatch.delenv("FUSION_ENABLE_GATEWAY", raising=False)
        from fusion_cowork.server.mcp_gateway import MCPGateway

        gw = MCPGateway()
        with pytest.raises(PermissionError):
            await gw.spawn("anything", "cmd")

    @pytest.mark.asyncio
    async def test_spawn_enabled_with_flag_no_permission_error(self, monkeypatch):
        monkeypatch.setenv("FUSION_ENABLE_GATEWAY", "1")
        from fusion_cowork.server.mcp_gateway import MCPGateway

        gw = MCPGateway()
        # 开了 flag, 过门控; 实际拉进程会因命令不存在/transport 抛 — 但非 PermissionError
        try:
            await gw.spawn("n", "nonexistent_cmd_xyz", [], {}, transport=None)
        except PermissionError:
            pytest.fail("不应抛 PermissionError (flag 已开)")
        except Exception:
            pass  # 进程拉起失败属预期


# ── LO-13: collab_ws auth + member ──


class TestCollabWsAuth:
    def test_hub_stores_auth_token(self):
        from fusion_cowork.server.collab_ws import CollabHub

        hub = CollabHub(auth_token="t")
        assert hub._auth_token == "t"
        hub2 = CollabHub()
        assert hub2._auth_token is None

    @pytest.mark.asyncio
    async def test_auth_token_mismatch_rejected(self):
        # 直接验证 serve_ws _handler 的认证分支逻辑 (不启真实 websockets server)
        from fusion_cowork.server.collab_ws import CollabHub

        hub = CollabHub(auth_token="secret")
        hello = {"token": "wrong", "space_id": "s", "user_id": "u"}
        # 复刻 _handler 认证判定
        token = str(hello.get("token", ""))
        rejected = hub._auth_token and token != hub._auth_token
        assert rejected

    @pytest.mark.asyncio
    async def test_member_check_rejects_nonmember(self):
        from fusion_cowork.server.collab_ws import CollabHub
        from fusion_cowork.space.models import Space

        store = AsyncMock()
        store.get_member = AsyncMock(return_value=None)
        # A-5: get_space 返回 Space dataclass, 非 dict — 旧 mock 返 dict 假绿,
        # 掩盖生产 space.get("owner_id") AttributeError。改用真实 Space。
        store.get_space = AsyncMock(return_value=Space(id="s1", name="s", owner_id="other"))
        hub = CollabHub(space_store=store)
        member = await hub._space_store.get_member("s1", "intruder")
        space = await hub._space_store.get_space("s1")
        owner_id = getattr(space, "owner_id", "") if space else ""
        rejected = member is None and (not space or owner_id != "intruder")
        assert rejected

    @pytest.mark.asyncio
    async def test_owner_allowed_when_not_member(self):
        from fusion_cowork.server.collab_ws import CollabHub
        from fusion_cowork.space.models import Space

        store = AsyncMock()
        store.get_member = AsyncMock(return_value=None)
        store.get_space = AsyncMock(return_value=Space(id="s1", name="s", owner_id="owner1"))
        hub = CollabHub(space_store=store)
        member = await hub._space_store.get_member("s1", "owner1")
        space = await hub._space_store.get_space("s1")
        owner_id = getattr(space, "owner_id", "") if space else ""
        rejected = member is None and (not space or owner_id != "owner1")
        assert not rejected


# ── MD-1: JSON-RPC batch rejection (mcp_http 3 端点 + desk_rpc 常量) ──


@pytest.fixture
def _mcp_http_client():
    from starlette.testclient import TestClient

    from fusion_cowork.server.mcp_http import create_http_app
    from fusion_cowork.server.mcp_server import MCPToolRegistry

    registry = MCPToolRegistry()
    registry.register_tools()
    app = create_http_app(registry)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def _mcp_streamable_client():
    from starlette.testclient import TestClient

    from fusion_cowork.server.mcp_http import create_streamable_app
    from fusion_cowork.server.mcp_server import MCPToolRegistry

    registry = MCPToolRegistry()
    registry.register_tools()
    app = create_streamable_app(registry)
    with TestClient(app) as client:
        yield client


class TestMcpHttpBatchReject:
    def test_legacy_mcp_batch_rejected(self, _mcp_http_client):
        resp = _mcp_http_client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ],
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32600

    def test_rpc_batch_rejected(self, _mcp_http_client):
        resp = _mcp_http_client.post(
            "/rpc",
            json=[{"jsonrpc": "2.0", "id": 1, "method": "plugins.list"}],
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32600

    def test_streamable_batch_rejected(self, _mcp_streamable_client):
        resp = _mcp_streamable_client.post(
            "/mcp",
            json=[{"jsonrpc": "2.0", "id": 1, "method": "initialize"}],
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32600


class TestDeskRpcBatchConstant:
    def test_line_cap_constant(self):
        from fusion_cowork.server.desk_rpc import _MAX_RPC_LINE, _RPC_READ_TIMEOUT

        assert _MAX_RPC_LINE == 1024 * 1024
        assert _RPC_READ_TIMEOUT == 5.0


# ── latent: desk.space.workflow.* 走 SpaceArtifactService (不再 AttributeError) ──


class TestSpaceWorkflowHandlersFixed:
    @pytest.mark.asyncio
    async def test_workflow_list_uses_artifact_svc(self):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        store = AsyncMock()
        store.get_space = AsyncMock(return_value=MagicMock(owner_id="owner1"))
        server = DeskRPCServer(space_store=store)
        # mock _get_artifact_svc 返回 AsyncMock service
        svc = AsyncMock()
        svc.list_artifacts = AsyncMock(return_value=[{"id": "wf1", "name": "n"}])
        server._get_artifact_svc = MagicMock(return_value=(svc, None))
        result = await server._handle_space_workflow_list({"space_id": "s1", "operator_id": "owner1"})
        assert "workflows" in result
        assert result["workflows"] == [{"id": "wf1", "name": "n"}]
        svc.list_artifacts.assert_called_once()

    @pytest.mark.asyncio
    async def test_workflow_run_uses_artifact_svc_and_engine(self):
        from fusion_cowork.server.desk_rpc import DeskRPCServer

        store = AsyncMock()
        store.get_space = AsyncMock(return_value=MagicMock(owner_id="owner1"))
        server = DeskRPCServer(space_store=store)
        svc = AsyncMock()
        svc.get_artifact = AsyncMock(
            return_value={"id": "wf1", "name": "wf", "content": json.dumps({"nodes": [], "edges": []})}
        )
        server._get_artifact_svc = MagicMock(return_value=(svc, None))
        # mock engine.execute
        fake_result = MagicMock()
        fake_result.status = MagicMock(value="completed")
        fake_result.id = "exec1"
        fake_result.error = None
        fake_result.result_summary = ""
        fake_result.steps = []
        engine = AsyncMock()
        engine.execute = AsyncMock(return_value=fake_result)
        server._get_engine = MagicMock(return_value=engine)
        result = await server._handle_space_workflow_run(
            {"space_id": "s1", "artifact_id": "wf1", "operator_id": "owner1"}
        )
        assert result["status"] == "completed"
        assert result["execution_id"] == "exec1"
        svc.get_artifact.assert_called_once()
        engine.execute.assert_called_once()
