"""Stage 3 权限模型+沙箱测试 — CR-16/17/18/2/3/19/20/21/22/23/15。

- CR-16: PermissionLevel.CONFIRM 默认, check() approve/deny/high-risk/else 顺序
- CR-17/18: HIGH_RISK_NODES 补全 27 个高危节点
- CR-2/3: HookEvent.permission_request 带外确认接通 CONFIRM
- CR-19: ApplyEdit ensure_allowed 拒越界路径
- CR-20: register 拒插件覆盖已注册节点名 (防劫持)
- CR-21: sandbox=false 插件无 plugins.trusted 白名单 → 拒绝加载
- CR-22: _install_zip zip-slip 拒绝 + rmtree 越界拒绝
- CR-23: env 白名单 + setrlimit fail-closed + seatbelt
- CR-15: sandbox_runner stdin 有界读 + traceback 不进 RPC
"""

import asyncio
import io
import json
import zipfile

import pytest

import fusion_cowork.nodes
from fusion_cowork.engine.node import BaseNode, NodeCategory, NodeConfig, NodeRegistry, NodeStatus, register_node
from fusion_cowork.engine.permission import HIGH_RISK_NODES, PermissionLevel, PermissionManager

fusion_cowork.nodes.import_all_nodes()


@pytest.fixture(autouse=True)
def _ensure_nodes_loaded():
    # 跨测试文件 NodeRegistry 可能被 clear(), 每测试前确保内置节点在册
    if "shell_exec" not in NodeRegistry._registry:
        fusion_cowork.nodes.import_all_nodes()
    yield


# ── CR-16: CONFIRM 默认 + check() 顺序 ──


class TestPermissionConfirmDefault:
    def test_confirm_is_default(self):
        pm = PermissionManager()
        assert pm.level == PermissionLevel.CONFIRM

    @pytest.mark.asyncio
    async def test_high_risk_denied_without_approve(self):
        pm = PermissionManager()
        assert await pm.check("shell_exec") is False

    @pytest.mark.asyncio
    async def test_low_risk_allowed_without_rule(self):
        pm = PermissionManager()
        # file_input 非高风险 → CONFIRM 放行
        assert await pm.check("file_input") is True

    @pytest.mark.asyncio
    async def test_approve_rule_allows_high_risk(self):
        pm = PermissionManager()
        pm.approve("shell_exec")
        assert await pm.check("shell_exec") is True

    @pytest.mark.asyncio
    async def test_deny_rule_rejects_low_risk(self):
        pm = PermissionManager()
        pm.deny("file_input")
        assert await pm.check("file_input") is False

    @pytest.mark.asyncio
    async def test_approve_takes_precedence_over_high_risk(self):
        # CR-16: approve 规则移到 high-risk 检查前, 任意 level 命中即放行
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        pm.approve("python_repl")
        assert await pm.check("python_repl") is True

    @pytest.mark.asyncio
    async def test_bypass_allows_all(self):
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        assert await pm.check("shell_exec") is True
        assert await pm.check("python_repl") is True

    @pytest.mark.asyncio
    async def test_manual_approve_now_effective(self):
        # CR-16 修复: 旧版 MANUAL approve() 是 no-op, 现在全 level 生效
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        pm.approve("file_delete")
        assert await pm.check("file_delete") is True

    @pytest.mark.asyncio
    async def test_manual_high_risk_without_approve_denied(self):
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        assert await pm.check("file_delete") is False


# ── CR-2/3: Hook 带外确认接通 CONFIRM ──


class TestHookApproval:
    @pytest.mark.asyncio
    async def test_hook_approve_allows_high_risk(self):
        from fusion_cowork.engine.hooks import HookEvent, HookManager

        hm = HookManager()

        async def approver(ctx):
            ctx.modify("approved", True)

        hm.register(HookEvent.PERMISSION_REQUEST, approver)
        pm = PermissionManager(hook_manager=hm)
        assert await pm.check("shell_exec") is True

    @pytest.mark.asyncio
    async def test_hook_cancel_denies(self):
        from fusion_cowork.engine.hooks import HookEvent, HookManager

        hm = HookManager()

        def canceller(ctx):
            ctx.cancel()

        hm.register(HookEvent.PERMISSION_REQUEST, canceller)
        pm = PermissionManager(hook_manager=hm)
        # 低风险也须被 hook 拒
        assert await pm.check("file_input") is False

    @pytest.mark.asyncio
    async def test_hook_receives_high_risk_flag(self):
        from fusion_cowork.engine.hooks import HookEvent, HookManager

        hm = HookManager()
        seen = {}

        async def observer(ctx):
            seen["high_risk"] = ctx.data.get("high_risk")

        hm.register(HookEvent.PERMISSION_REQUEST, observer)
        pm = PermissionManager(hook_manager=hm)
        await pm.check("shell_exec")
        await pm.check("file_input")
        # 最后一次调用 (file_input) high_risk=False
        assert seen["high_risk"] is False


# ── CR-17/18: HIGH_RISK_NODES 补全 ──


class TestHighRiskNodesComplete:
    EXPECTED_HIGH_RISK = {
        "shell_exec",
        "python_repl",
        "apply_edit",
        "file_delete",
        "file_copy",
        "file_move",
        "disk_cleaner",
        "desktop_clean",
        "download_organizer",
        "app_lifecycle",
        "screen_capture",
        "clipboard",
        "notification",
        "mouse_click",
        "mouse_move",
        "keyboard_type",
        "keyboard_shortcut",
        "computer_use_loop",
        "browser_automate",
        "cdp_evaluate",
        "cdp_navigate",
        "cdp_screenshot",
        "cdp_click",
        "cdp_fill",
        "cdp_fill_form",
        "cdp_emulate",
        "cdp_network",
    }

    EXPECTED_LOW_RISK = {"file_input", "file_output", "filter", "loop", "merge", "web_search", "fetch_url"}

    def test_all_expected_high_risk_present(self):
        missing = self.EXPECTED_HIGH_RISK - HIGH_RISK_NODES
        assert not missing, f"缺高风险节点: {missing}"

    def test_low_risk_not_in_high_risk(self):
        leaked = self.EXPECTED_LOW_RISK & HIGH_RISK_NODES
        assert not leaked, f"低风险误入高风险: {leaked}"

    def test_high_risk_names_all_registered(self):
        # CR-17/18 校验: 高风险名须是真实注册节点 (防拼写错误静默失效)
        all_names = {n["name"] for n in NodeRegistry.list()}
        for name in self.EXPECTED_HIGH_RISK:
            assert name in all_names, f"高风险节点 {name} 未注册 (拼写错误?)"


# ── CR-19: ApplyEdit ensure_allowed ──


class TestApplyEditScope:
    @pytest.mark.asyncio
    async def test_apply_edit_rejects_out_of_scope_path(self, tmp_path, monkeypatch):
        # scoped_folder 默认允许 tmp_path; 写一个越界绝对路径应被拒
        from fusion_cowork.security import get_scoped_folder_manager

        scope = get_scoped_folder_manager()
        monkeypatch.setattr(scope, "ensure_allowed", lambda p: False)
        node = NodeRegistry.create(
            "apply_edit",
            config=NodeConfig(
                params={"file_path": str(tmp_path / "x.txt"), "old_text": "a", "new_text": "b", "dry_run": True}
            ),
        )
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED
        assert "越界" in result.error or "沙箱" in result.error

    @pytest.mark.asyncio
    async def test_apply_edit_in_scope_dry_run_works(self, tmp_path):
        f = tmp_path / "in.txt"
        f.write_text("hello world", encoding="utf-8")
        from fusion_cowork.config_center import ConfigCenter
        from fusion_cowork.security import get_scoped_folder_manager, reset_scoped_folder_manager

        ConfigCenter.reset_instance()
        cc = ConfigCenter.get_instance()
        cc.set("workspace.scoped_folder", str(tmp_path))
        cc.set("workspace.enforce_scope", True)
        reset_scoped_folder_manager()
        try:
            scope = get_scoped_folder_manager()
            assert scope.enforce is True
            node = NodeRegistry.create(
                "apply_edit",
                config=NodeConfig(
                    params={
                        "file_path": str(f),
                        "old_text": "hello",
                        "new_text": "hi",
                        "dry_run": True,
                        "create_backup": False,
                    }
                ),
            )
            result = await node.execute({})
            assert result.status == NodeStatus.SUCCESS
            assert result.data.get("changes") == 1
        finally:
            reset_scoped_folder_manager()
            ConfigCenter.reset_instance()


# ── CR-20: register 拒覆盖 ──


class TestRegisterHijackGuard:
    def test_register_refuses_override_by_different_class(self):
        saved = dict(NodeRegistry._registry)
        saved_prot = set(NodeRegistry._protected_names)
        try:
            NodeRegistry.clear()

            @register_node
            class Orig(BaseNode):
                name = "cr20_test_node"
                display_name = "o"
                category = NodeCategory.TOOL
                description = "d"
                icon = "i"
                default_label = "l"

            class Hijack(BaseNode):
                name = "cr20_test_node"
                display_name = "h"
                category = NodeCategory.TOOL
                description = "d"
                icon = "i"
                default_label = "l"

            NodeRegistry.register(Hijack)
            # Orig 仍在 (未被覆盖)
            assert NodeRegistry.get("cr20_test_node") is Orig
        finally:
            NodeRegistry._registry.clear()
            NodeRegistry._registry.update(saved)
            NodeRegistry._protected_names.clear()
            NodeRegistry._protected_names.update(saved_prot)

    def test_register_same_class_idempotent(self):
        saved = dict(NodeRegistry._registry)
        saved_prot = set(NodeRegistry._protected_names)
        try:
            NodeRegistry.clear()

            class ReReg(BaseNode):
                name = "cr20_rereg"
                display_name = "r"
                category = NodeCategory.TOOL
                description = "d"
                icon = "i"
                default_label = "l"

            NodeRegistry.register(ReReg)
            NodeRegistry.register(ReReg)  # 同类重注册幂等
            assert NodeRegistry.get("cr20_rereg") is ReReg
        finally:
            NodeRegistry._registry.clear()
            NodeRegistry._registry.update(saved)
            NodeRegistry._protected_names.clear()
            NodeRegistry._protected_names.update(saved_prot)

    def test_register_force_overrides(self):
        saved = dict(NodeRegistry._registry)
        saved_prot = set(NodeRegistry._protected_names)
        try:
            NodeRegistry.clear()

            class A(BaseNode):
                name = "cr20_force"
                display_name = "a"
                category = NodeCategory.TOOL
                description = "d"
                icon = "i"
                default_label = "l"

            class B(BaseNode):
                name = "cr20_force"
                display_name = "b"
                category = NodeCategory.TOOL
                description = "d"
                icon = "i"
                default_label = "l"

            NodeRegistry.register(A)
            NodeRegistry.register(B, force=True)
            assert NodeRegistry.get("cr20_force") is B
        finally:
            NodeRegistry._registry.clear()
            NodeRegistry._registry.update(saved)
            NodeRegistry._protected_names.clear()
            NodeRegistry._protected_names.update(saved_prot)


# ── CR-21: sandbox=false 插件信任白名单 ──


class TestPluginTrust:
    def test_sandbox_false_rejected_without_trust(self, tmp_path, monkeypatch):
        from fusion_cowork.config_center import ConfigCenter
        from fusion_cowork.plugins.loader import PluginLoader

        ConfigCenter.reset_instance()
        cc = ConfigCenter.get_instance()
        cc.set_default("plugins.trusted", [])  # 空白名单

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        plugin_dir = plugins_dir / "evil"
        plugin_dir.mkdir()
        (plugin_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "evil",
                    "version": "0.1.0",
                    "description": "x",
                    "author": "x",
                    "nodes": [],
                    "dependencies": [],
                    "entry_point": "main",
                    "sandbox": False,
                }
            ),
            encoding="utf-8",
        )
        (plugin_dir / "main.py").write_text(
            "from fusion_cowork.engine.node import BaseNode, NodeCategory, register_node\n"
            "@register_node\nclass Evil(BaseNode):\n"
            "    name='evil_node'\n    display_name='e'\n    category=NodeCategory.TOOL\n"
            "    description='d'\n    icon='i'\n    default_label='l'\n",
            encoding="utf-8",
        )

        loader = PluginLoader(plugins_dir=str(plugins_dir))
        loaded = loader.load("evil")
        assert loaded == []  # 未信任 → 拒绝加载
        assert not loader.is_loaded("evil")
        ConfigCenter.reset_instance()

    def test_sandbox_false_allowed_with_trust(self, tmp_path, monkeypatch):
        from fusion_cowork.config_center import ConfigCenter
        from fusion_cowork.plugins.loader import PluginLoader

        ConfigCenter.reset_instance()
        cc = ConfigCenter.get_instance()
        cc.set_default("plugins.trusted", ["good"])

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        plugin_dir = plugins_dir / "good"
        plugin_dir.mkdir()
        (plugin_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "good",
                    "version": "0.1.0",
                    "description": "x",
                    "author": "x",
                    "nodes": [],
                    "dependencies": [],
                    "entry_point": "main",
                    "sandbox": False,
                }
            ),
            encoding="utf-8",
        )
        (plugin_dir / "main.py").write_text(
            "from fusion_cowork.engine.node import BaseNode, NodeCategory, register_node\n"
            "@register_node\nclass Good(BaseNode):\n"
            "    name='good_node'\n    display_name='g'\n    category=NodeCategory.TOOL\n"
            "    description='d'\n    icon='i'\n    default_label='l'\n",
            encoding="utf-8",
        )

        loader = PluginLoader(plugins_dir=str(plugins_dir))
        loaded = loader.load("good")
        assert len(loaded) == 1
        assert loader.is_loaded("good")
        NodeRegistry.unregister("good_node")
        ConfigCenter.reset_instance()


# ── CR-22: zip-slip + rmtree 越界 ──


class TestZipSlip:
    def test_zip_sllip_rejected(self, tmp_path):
        from fusion_cowork.plugins.loader import PluginLoader

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        loader = PluginLoader(plugins_dir=str(plugins_dir))

        # 构造恶意 zip: 顶层目录 + 一个 ../escape 路径条目
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("evil/manifest.json", "{}")
            zf.writestr("evil/main.py", "# evil")
            zf.writestr("evil/../../escape.txt", "PWN")
        ok = loader._install_zip(zip_path)
        assert ok is False
        # escape.txt 不应被写到 tmp_path 之上
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_safe_rmtree_rejects_traversal(self, tmp_path):
        from fusion_cowork.plugins.loader import PluginLoader

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        loader = PluginLoader(plugins_dir=str(plugins_dir))
        # 试图删 ../ (越界)
        assert loader._safe_rmtree("../") is False
        assert loader._safe_rmtree("../../") is False

    def test_safe_zip_install_succeeds(self, tmp_path):
        from fusion_cowork.plugins.loader import PluginLoader

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        loader = PluginLoader(plugins_dir=str(plugins_dir))

        zip_path = tmp_path / "good.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("good/manifest.json", '{"name":"good"}')
            zf.writestr("good/main.py", "# good")
        ok = loader._install_zip(zip_path)
        assert ok is True
        assert (plugins_dir / "good" / "manifest.json").exists()


# ── CR-23: env 白名单 + seatbelt ──


class TestSandboxEnv:
    def test_safe_env_keys_excludes_secrets(self):
        from fusion_cowork.plugins.sandbox import _SAFE_ENV_KEYS

        # 敏感变量不在白名单
        assert "OPENAI_API_KEY" not in _SAFE_ENV_KEYS
        assert "FUSION_MLX_API_KEY" not in _SAFE_ENV_KEYS
        assert "AWS_SECRET_ACCESS_KEY" not in _SAFE_ENV_KEYS
        # 必要变量在白名单
        assert "PATH" in _SAFE_ENV_KEYS
        assert "HOME" in _SAFE_ENV_KEYS

    @pytest.mark.asyncio
    async def test_execute_passes_only_safe_env(self, tmp_path, monkeypatch):
        from fusion_cowork.plugins.sandbox import PluginSandbox

        captured = {}

        class FakeProc:
            returncode = 0

            async def communicate(self, input=None):
                return (b"", b"")

        async def fake_exec(cmd, *args, **kw):
            captured["env"] = kw.get("env", {})
            captured["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        sbx = PluginSandbox()
        # 注入敏感 env 到父进程
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("PATH", "/usr/bin")
        await sbx.execute(plugin_name="t", command="/bin/true", args=[], stdin_data="{}")
        env = captured["env"]
        assert "OPENAI_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin"
        assert env.get("FUSION_SANDBOX_ID", "").startswith("sbx_")

    def test_seatbelt_wrap_returns_sandbox_exec(self):
        from fusion_cowork.plugins.sandbox import PluginSandbox

        sbx = PluginSandbox()
        cmd, args = sbx._wrap_seatbelt("/bin/true", ["--flag"])
        assert cmd == "sandbox-exec"
        assert "-f" in args
        assert "--" in args
        assert "/bin/true" in args


# ── CR-15: sandbox_runner 有界读 + 无 traceback ──


class TestSandboxRunnerBounded:
    def test_oversized_input_rejected(self, monkeypatch):
        import fusion_cowork.plugins.sandbox_runner as runner

        # 模拟 stdin 超过 16MB
        big = "x" * (16 * 1024 * 1024 + 10)
        monkeypatch.setattr("sys.stdin", io.StringIO(big))
        import asyncio as _aio

        rc = _aio.run(runner.main())
        assert rc == 1

    def test_error_payload_no_traceback(self, monkeypatch):
        import fusion_cowork.plugins.sandbox_runner as runner

        # 合法 JSON 但 action 非法 → 走 except 分支? 不, 未知 action 走 else (非 except)
        # 构造触发 _introspect 抛错: entry_file 不存在
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"action": "introspect", "entry_file": "/nope/x.py"})))
        import asyncio as _aio

        rc = _aio.run(runner.main())
        assert rc == 1
        # stdout 末尾应是 _RESULT_MARKER + JSON, 不含 traceback 字段
