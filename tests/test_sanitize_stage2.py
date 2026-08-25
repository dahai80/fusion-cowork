"""Stage 2 输入净化测试 — CR-6/7/13bc/CR-4/CR-14/MD-18/HI-11。

- CR-6/7: ShellExec 默认 exec 列表 (不起 shell), shell=True 显式
- MD-18: PythonREPL AST walk 拒危险 import/call
- CR-13c: PythonREPL 变量名标识符校验
- CR-13b: nl_parser 节点名剔除未注册
- CR-4: store.update_space/update_member 列白名单
- CR-14: cdp navigate scheme + evaluate_js gate + host 限 localhost + token header
- HI-11: AppleScript 转义 (单引号注入 / 双引号注入)
"""

import pytest

# 触发节点注册 (cdp/macos 模块在 import 时 @register_node)
import fusion_cowork.nodes
from fusion_cowork.engine.node import NodeConfig, NodeRegistry, NodeStatus
from fusion_cowork.nodes.tools.tool_nodes import (
    _check_python_code,
    _validate_repl_variables,
)

fusion_cowork.nodes.import_all_nodes()


# ── CR-6/7: ShellExec exec 列表 (默认 shell=False) ──


class TestShellExecExecList:
    @pytest.mark.asyncio
    async def test_default_shell_false_exec_list(self):
        # shell=False (默认) 走 exec 列表, 命令含 shell 元字符不会注入
        node = NodeRegistry.create(
            "shell_exec",
            config=NodeConfig(
                params={
                    "command": "echo safe_arg",
                    "timeout": 5,
                    "capture_output": True,
                }
            ),
        )
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert "safe_arg" in result.data.get("stdout", "")

    @pytest.mark.asyncio
    async def test_shell_false_rejects_shell_metachar_as_arg(self):
        # shell=False: "echo hi; echo PWN" 被 shlex 拆为 ['echo','hi;','echo','PWN']
        # → echo 打印 "hi; echo PWN", 第二条 echo 不执行 (无 shell 注入)
        node = NodeRegistry.create(
            "shell_exec",
            config=NodeConfig(
                params={
                    "command": "echo hi; echo PWN",
                    "timeout": 5,
                    "capture_output": True,
                }
            ),
        )
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        stdout = result.data.get("stdout", "")
        assert "hi; echo PWN" in stdout

    @pytest.mark.asyncio
    async def test_shell_true_explicit_runs_shell(self):
        # 显式 shell=True 仍可执行 (保留向后兼容, 记 WARNING)
        node = NodeRegistry.create(
            "shell_exec",
            config=NodeConfig(
                params={
                    "command": "echo $((1+1))",
                    "timeout": 5,
                    "capture_output": True,
                    "shell": True,
                }
            ),
        )
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        assert "2" in result.data.get("stdout", "")

    @pytest.mark.asyncio
    async def test_empty_command_after_split_fails(self):
        node = NodeRegistry.create(
            "shell_exec",
            config=NodeConfig(
                params={
                    "command": "   ",
                    "timeout": 5,
                    "capture_output": True,
                }
            ),
        )
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED


# ── MD-18: PythonREPL AST walk ──


class TestPythonCodeASTCheck:
    def test_blocks_dangerous_import(self):
        # subprocess 在黑名单 → 拒
        assert _check_python_code("import subprocess") is not None

    def test_os_system_attr_blocked_even_if_os_imported(self):
        # os 不在黑名单, 但 os.system() 调用被 AST 拒
        assert _check_python_code("import os\nos.system('ls')") is not None

    def test_os_safe_call_allowed(self):
        # os.getcwd() 安全 → 放行
        assert _check_python_code("import os\nos.getcwd()") is None

    def test_blocks_dangerous_from_import(self):
        assert _check_python_code("from subprocess import run") is not None

    def test_blocks_os_system_call(self):
        assert _check_python_code("import os\nos.system('ls')") is not None

    def test_blocks_subprocess_call(self):
        assert _check_python_code("import subprocess\nsubprocess.run(['ls'])") is not None

    def test_allows_safe_code(self):
        assert _check_python_code("x = 1 + 1\nprint(x)") is None

    def test_allows_math_import(self):
        assert _check_python_code("import math\nmath.sqrt(4)") is None

    def test_syntax_error_passes_through(self):
        # 语法错误交子进程报错, 不在此拦截
        assert _check_python_code("this is not python !!!") is None


# ── CR-13c: PythonREPL 变量名校验 ──


class TestReplVarValidation:
    def test_valid_identifiers_pass(self):
        assert _validate_repl_variables({"x": 1, "_y": 2, "z_1": 3}) is None

    def test_rejects_dunder_prefix(self):
        err = _validate_repl_variables({"__import__": 1})
        assert err is not None and "dunder" in err

    def test_rejects_non_identifier(self):
        err = _validate_repl_variables({"bad name": 1})
        assert err is not None and "标识符" in err

    def test_rejects_digit_start(self):
        err = _validate_repl_variables({"1var": 1})
        assert err is not None and "标识符" in err

    def test_rejects_injection_chars(self):
        err = _validate_repl_variables({"x; rm -rf /": 1})
        assert err is not None and "标识符" in err

    @pytest.mark.asyncio
    async def test_repl_node_rejects_bad_var_name(self):
        node = NodeRegistry.create(
            "python_repl",
            config=NodeConfig(
                params={
                    "code": "x",
                    "variables": {"x; import os": 1},
                    "timeout": 5,
                }
            ),
        )
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED


# ── CR-4: store 列白名单 ──


class TestStoreColumnWhitelist:
    @pytest.mark.asyncio
    async def test_update_space_ignores_non_whitelist_column(self, tmp_path):
        from fusion_cowork.space.models import Space, SpaceConfig, SpaceStatus
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp1",
            name="t",
            description="",
            owner_id="u1",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        # 非白名单列 (SQL 注入尝试) 应被拒, 合法列更新生效
        updated = await store.update_space(
            "sp1",
            name="renamed",
            **{"name'; DROP TABLE spaces--": "evil"},
        )
        assert updated is not None
        assert updated.name == "renamed"
        await store.close()

    @pytest.mark.asyncio
    async def test_update_member_ignores_non_whitelist_column(self, tmp_path):
        from fusion_cowork.space.models import (
            Space,
            SpaceConfig,
            SpaceMember,
            SpaceRole,
            SpaceStatus,
        )
        from fusion_cowork.space.store import SpaceStore

        store = SpaceStore(data_dir=str(tmp_path))
        await store.initialize()
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        sp = Space(
            id="sp1",
            name="t",
            description="",
            owner_id="u1",
            status=SpaceStatus.ACTIVE,
            kb_bind_mode="new_private",
            kb_id=None,
            collab_mode="local",
            config=SpaceConfig(),
            created_at=now,
            updated_at=now,
        )
        await store.create_space(sp)
        await store.add_member(
            SpaceMember(
                space_id="sp1",
                user_id="u2",
                role=SpaceRole.MEMBER,
                display_name="u2",
                joined_at=now,
                last_active=now,
            )
        )
        updated = await store.update_member(
            "sp1",
            "u2",
            display_name="new",
            **{"role'; DROP TABLE members--": "evil"},
        )
        assert updated is not None
        assert updated.display_name == "new"
        await store.close()


# ── CR-14: cdp_client scheme + gate + host + token ──


class TestCDPSecurity:
    def test_host_rejected_non_localhost(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        with pytest.raises(ValueError, match="非本机"):
            CDPClient(host="192.168.1.1", port=9222)

    def test_localhost_hosts_allowed(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        for h in ["127.0.0.1", "localhost", "::1"]:
            c = CDPClient(host=h, port=9222)
            assert c.host == h

    def test_token_stored(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        c = CDPClient(host="127.0.0.1", port=9222, token="sek")
        assert c.token == "sek"

    @pytest.mark.asyncio
    async def test_navigate_rejects_file_scheme(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        c = CDPClient(host="127.0.0.1", port=9222)
        with pytest.raises(ValueError, match="scheme"):
            await c.navigate("file:///etc/passwd")

    @pytest.mark.asyncio
    async def test_navigate_rejects_javascript_scheme(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        c = CDPClient(host="127.0.0.1", port=9222)
        with pytest.raises(ValueError, match="scheme"):
            await c.navigate("javascript:alert(1)")

    @pytest.mark.asyncio
    async def test_navigate_rejects_data_scheme(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        c = CDPClient(host="127.0.0.1", port=9222)
        with pytest.raises(ValueError, match="scheme"):
            await c.navigate("data:text/html,<script>1</script>")

    @pytest.mark.asyncio
    async def test_evaluate_js_rejected_without_confirm(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        c = CDPClient(host="127.0.0.1", port=9222)
        with pytest.raises(PermissionError, match="确认"):
            await c.evaluate_js("1+1")

    def test_confirm_js_eval_sets_flag(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        c = CDPClient(host="127.0.0.1", port=9222)
        assert c._js_eval_confirmed is False
        c.confirm_js_eval()
        assert c._js_eval_confirmed is True

    @pytest.mark.asyncio
    async def test_evaluate_node_rejected_without_allow_js(self):
        node = NodeRegistry.create(
            "cdp_evaluate",
            config=NodeConfig(params={"script": "1+1"}),
        )
        # 不连真 CDP, allow_js 缺省 False → 在连接前即被拒
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED
        assert "授权" in result.error or "确认" in result.error

    @pytest.mark.asyncio
    async def test_get_ws_url_sends_token_header(self, tmp_path, monkeypatch):
        from fusion_cowork.nodes.browser import cdp_client as mod

        c = mod.CDPClient(host="127.0.0.1", port=9222, token="mytok")

        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"}]

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                captured["url"] = url
                captured["headers"] = headers or {}
                return FakeResp()

        monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
        ws = await c._get_ws_url()
        assert ws == "ws://127.0.0.1:9222/devtools/page/1"
        assert captured["headers"].get("Authorization") == "Bearer mytok"


# ── HI-11: AppleScript 转义 (shell 单引号 + AppleScript 双引号) ──


class TestAppleScriptEscaping:
    def _shell_quote_count(self, cmd: str) -> int:
        # 将 break-quote 序列 '"'"' 视为占位, 计剩余裸单引号 (须偶数 = 边界闭合)
        return cmd.replace("'\"'\"'", "\x00").count("'")

    @pytest.mark.asyncio
    async def test_notification_single_quote_not_injected(self, monkeypatch):
        import fusion_cowork.nodes.macos.system_nodes as sn

        captured = {}

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def fake_shell(cmd, **kw):
            captured["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr(sn.asyncio, "create_subprocess_shell", fake_shell)
        node = sn.NotificationNode(config=NodeConfig(params={"title": "x' ; echo PWN", "message": "m"}))
        result = await node.execute({})
        assert result.status == NodeStatus.SUCCESS
        # 裸单引号须偶数 (shell 引号边界完整闭合, 注入的 ; echo PWN 落在字符串内)
        assert self._shell_quote_count(captured["cmd"]) % 2 == 0

    @pytest.mark.asyncio
    async def test_app_lifecycle_quote_not_injected(self, monkeypatch):
        import fusion_cowork.nodes.macos.system_nodes as sn

        captured = []

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"false", b"")

        async def fake_shell(cmd, **kw):
            captured.append(cmd)
            return FakeProc()

        monkeypatch.setattr(sn.asyncio, "create_subprocess_shell", fake_shell)
        node = sn.AppLifecycleNode(config=NodeConfig(params={"action": "check", "app_name": 'Calc" ; say hi'}))
        await node.execute({})
        # osascript 调用 (含 app_name 插值) 的双引号须被转义, 单引号边界闭合
        osascript_cmd = next((c for c in captured if "osascript" in c), "")
        assert '\\"' in osascript_cmd
        assert self._shell_quote_count(osascript_cmd) % 2 == 0
        # pgrep 调用的单引号边界也须闭合 (第二处注入点)
        pgrep_cmd = next((c for c in captured if "pgrep" in c), "")
        assert self._shell_quote_count(pgrep_cmd) % 2 == 0
