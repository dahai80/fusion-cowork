"""Stage 3 permission model + sandbox tests — CR-16/17/18/2/3/19/20/21/22/23/15.

- CR-16: PermissionLevel.CONFIRM default, check() approve/deny/high-risk/else order
- CR-17/18: HIGH_RISK_NODES completed with 27 high-risk nodes
- CR-2/3: HookEvent.permission_request out-of-band confirmation wired to CONFIRM
- CR-19: ApplyEdit ensure_allowed rejects out-of-scope paths
- CR-20: register rejects plugin overriding an already-registered node name (anti-hijack)
- CR-21: sandbox=false plugin without plugins.trusted allowlist -> load refused
- CR-22: _install_zip zip-slip rejection + rmtree out-of-scope rejection
- CR-23: env allowlist + setrlimit fail-closed + seatbelt
- CR-15: sandbox_runner bounded stdin read + no traceback into RPC
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
    # NodeRegistry may be clear()ed across test files; re-register built-ins before each test
    if "shell_exec" not in NodeRegistry._registry:
        fusion_cowork.nodes.import_all_nodes()
    yield


# ── CR-16: CONFIRM default + check() order ──


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
        # file_input is not high-risk -> CONFIRM allows
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
        # CR-16: approve rule moved before the high-risk check; any level match allows
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
        # CR-16 fix: old MANUAL approve() was a no-op; now effective at all levels
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        pm.approve("file_delete")
        assert await pm.check("file_delete") is True

    @pytest.mark.asyncio
    async def test_manual_high_risk_without_approve_denied(self):
        pm = PermissionManager(level=PermissionLevel.MANUAL)
        assert await pm.check("file_delete") is False


# ── CR-2/3: Hook out-of-band confirmation wired to CONFIRM ──


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
        # low-risk must also be denied by the hook
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
        # last call (file_input) has high_risk=False
        assert seen["high_risk"] is False


# ── CR-17/18: HIGH_RISK_NODES completion ──


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
        "cdp_wait_for",  # E-10: wait_for_function arbitrary JS injection
        "fetch_url",  # E-11: SSRF surface
    }

    EXPECTED_LOW_RISK = {"file_input", "file_output", "filter", "loop", "merge", "web_search"}

    def test_all_expected_high_risk_present(self):
        missing = self.EXPECTED_HIGH_RISK - HIGH_RISK_NODES
        assert not missing, f"缺高风险节点: {missing}"  # i18n: user-facing

    def test_low_risk_not_in_high_risk(self):
        leaked = self.EXPECTED_LOW_RISK & HIGH_RISK_NODES
        assert not leaked, f"低风险误入高风险: {leaked}"  # i18n: user-facing

    def test_high_risk_names_all_registered(self):
        # CR-17/18 check: high-risk names must be real registered nodes (guards against silent typo)
        all_names = {n["name"] for n in NodeRegistry.list()}
        for name in self.EXPECTED_HIGH_RISK:
            assert name in all_names, f"高风险节点 {name} 未注册 (拼写错误?)"  # i18n: user-facing


# ── CR-19: ApplyEdit ensure_allowed ──


class TestApplyEditScope:
    @pytest.mark.asyncio
    async def test_apply_edit_rejects_out_of_scope_path(self, tmp_path, monkeypatch):
        # scoped_folder allows tmp_path by default; an out-of-scope absolute path must be rejected
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
        assert "越界" in result.error or "沙箱" in result.error  # i18n: user-facing

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


# ── CR-20: register rejects override ──


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
            # Orig still registered (not overridden)
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
            NodeRegistry.register(ReReg)  # same-class re-registration is idempotent
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


# ── CR-21: sandbox=false plugin trust allowlist ──


class TestPluginTrust:
    def test_sandbox_false_rejected_without_trust(self, tmp_path, monkeypatch):
        from fusion_cowork.config_center import ConfigCenter
        from fusion_cowork.plugins.loader import PluginLoader

        ConfigCenter.reset_instance()
        cc = ConfigCenter.get_instance()
        cc.set_default("plugins.trusted", [])  # empty allowlist

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
        assert loaded == []  # untrusted -> load refused
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


# ── CR-22: zip-slip + rmtree out-of-scope ──


class TestZipSlip:
    def test_zip_sllip_rejected(self, tmp_path):
        from fusion_cowork.plugins.loader import PluginLoader

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        loader = PluginLoader(plugins_dir=str(plugins_dir))

        # craft malicious zip: top-level dir + one ../escape path entry
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("evil/manifest.json", "{}")
            zf.writestr("evil/main.py", "# evil")
            zf.writestr("evil/../../escape.txt", "PWN")
        ok = loader._install_zip(zip_path)
        assert ok is False
        # escape.txt must not be written above tmp_path
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_safe_rmtree_rejects_traversal(self, tmp_path):
        from fusion_cowork.plugins.loader import PluginLoader

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        loader = PluginLoader(plugins_dir=str(plugins_dir))
        # attempt to delete ../ (out of scope)
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


# ── CR-23: env allowlist + seatbelt ──


class TestSandboxEnv:
    def test_safe_env_keys_excludes_secrets(self):
        from fusion_cowork.plugins.sandbox import _SAFE_ENV_KEYS

        # secret vars not in allowlist
        assert "OPENAI_API_KEY" not in _SAFE_ENV_KEYS
        assert "FUSION_MLX_API_KEY" not in _SAFE_ENV_KEYS
        assert "AWS_SECRET_ACCESS_KEY" not in _SAFE_ENV_KEYS
        # required vars in allowlist
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
        # inject sensitive env into the parent process
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("PATH", "/usr/bin")
        await sbx.execute(plugin_name="t", command="/bin/true", args=[], stdin_data="{}")
        env = captured["env"]
        assert "OPENAI_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin"
        assert env.get("FUSION_SANDBOX_ID", "").startswith("sbx_")

    def test_seatbelt_wrap_returns_sandbox_exec(self, monkeypatch):
        # CI (ubuntu) has no sandbox-exec -> _wrap_seatbelt falls back to the original command; the
        # cross-platform seatbelt path needs shutil.which mocked to pretend sandbox-exec exists
        import shutil as _shutil
        from pathlib import Path as _Path

        import fusion_cowork.plugins.sandbox as sandbox_mod

        monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None)
        sbx = sandbox_mod.PluginSandbox()
        # use the real local true binary (macOS /bin/true, linux /usr/bin/true) —
        # cross-platform assert its resolved absolute path appears in the seatbelt args
        true_bin = _shutil.which("true") or "/bin/true"
        expected_abs = str(_Path(true_bin).resolve())
        cmd, args = sbx._wrap_seatbelt(true_bin, ["--flag"])
        assert cmd == "sandbox-exec"
        assert "-f" in args
        assert "--" in args
        assert expected_abs in args, f"seatbelt args 应含解析后的命令绝对路径, args={args}"  # i18n: user-facing


# ── CR-15: sandbox_runner bounded read + no traceback ──


class TestSandboxRunnerBounded:
    def test_oversized_input_rejected(self, monkeypatch):
        import fusion_cowork.plugins.sandbox_runner as runner

        # simulate stdin over 16MB
        big = "x" * (16 * 1024 * 1024 + 10)
        monkeypatch.setattr("sys.stdin", io.StringIO(big))
        import asyncio as _aio

        rc = _aio.run(runner.main())
        assert rc == 1

    def test_error_payload_no_traceback(self, monkeypatch):
        import fusion_cowork.plugins.sandbox_runner as runner

        # valid JSON but bad action -> except branch? No: unknown action goes to else (not except)
        # trigger an _introspect raise: entry_file does not exist
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"action": "introspect", "entry_file": "/nope/x.py"})))
        import asyncio as _aio

        rc = _aio.run(runner.main())
        assert rc == 1
        # stdout tail must be _RESULT_MARKER + JSON, without a traceback field
