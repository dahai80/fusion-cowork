"""Stage 2 input sanitization tests — CR-6/7/13bc/CR-4/CR-14/MD-18/HI-11.

- CR-6/7: ShellExec default exec list (no shell), shell=True explicit
- MD-18: PythonREPL AST walk rejects dangerous import/call
- CR-13c: PythonREPL variable name identifier validation
- CR-13b: nl_parser strips unregistered node names
- CR-4: store.update_space/update_member column whitelist
- CR-14: cdp navigate scheme + evaluate_js gate + host limited to localhost + token header
- HI-11: AppleScript escaping (single-quote injection / double-quote injection)
"""

import pytest

# Trigger node registration (cdp/macos modules @register_node at import time)
import fusion_cowork.nodes
from fusion_cowork.engine.node import NodeConfig, NodeRegistry, NodeStatus
from fusion_cowork.nodes.tools.tool_nodes import (
    _check_python_code,
    _validate_repl_variables,
)

fusion_cowork.nodes.import_all_nodes()


# ── CR-6/7: ShellExec exec list (default shell=False) ──


class TestShellExecExecList:
    @pytest.mark.asyncio
    async def test_default_shell_false_exec_list(self):
        # shell=False (default) goes through the exec list, commands with shell metacharacters are not injected
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
        # shell=False: "echo hi; echo PWN" used to be split by shlex into literal argv (injection-safe but silently wrong);
        # issue #319: metacharacters are rejected loudly up front under shell=False — callers either simplify the command or set shell=True explicitly
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
        assert result.status == NodeStatus.FAILED
        assert "shell operator" in (result.error or "")
        assert "shell=true" in (result.error or "")

    @pytest.mark.asyncio
    async def test_shell_true_explicit_runs_shell(self):
        # Explicit shell=True still executes (kept for backward compat, logs WARNING)
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
        # subprocess is blacklisted → reject
        assert _check_python_code("import subprocess") is not None

    def test_os_system_attr_blocked_even_if_os_imported(self):
        # os is not blacklisted, but the os.system() call is rejected by the AST walk
        assert _check_python_code("import os\nos.system('ls')") is not None

    def test_os_safe_call_allowed(self):
        # os.getcwd() is safe → allow
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
        # Syntax errors are surfaced by the subprocess, not intercepted here
        assert _check_python_code("this is not python !!!") is None


# ── CR-13c: PythonREPL variable name validation ──


class TestReplVarValidation:
    def test_valid_identifiers_pass(self):
        assert _validate_repl_variables({"x": 1, "_y": 2, "z_1": 3}) is None

    def test_rejects_dunder_prefix(self):
        err = _validate_repl_variables({"__import__": 1})
        assert err is not None and "dunder" in err

    def test_rejects_non_identifier(self):
        err = _validate_repl_variables({"bad name": 1})
        # i18n: user-facing
        assert err is not None and "标识符" in err

    def test_rejects_digit_start(self):
        err = _validate_repl_variables({"1var": 1})
        # i18n: user-facing
        assert err is not None and "标识符" in err

    def test_rejects_injection_chars(self):
        err = _validate_repl_variables({"x; rm -rf /": 1})
        # i18n: user-facing
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


# ── CR-4: store column whitelist ──


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
        # Non-whitelisted columns (SQL injection attempt) should be rejected, legitimate column updates take effect
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

        # i18n: user-facing
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
        # i18n: user-facing
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
        # No real CDP connection, allow_js defaults False → rejected before connecting
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED
        # i18n: user-facing
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


# ── HI-11: AppleScript escaping (shell single quotes + AppleScript double quotes) ──


class TestAppleScriptEscaping:
    def _shell_quote_count(self, cmd: str) -> int:
        # Treat the break-quote sequence '"'"' as a placeholder, count remaining bare single quotes (must be even = boundaries closed)
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
        # Bare single quotes must be even (shell quote boundaries fully closed, injected ; echo PWN stays inside the string)
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
        # Double quotes in the osascript call (with app_name interpolation) must be escaped, single-quote boundaries closed
        osascript_cmd = next((c for c in captured if "osascript" in c), "")
        assert '\\"' in osascript_cmd
        assert self._shell_quote_count(osascript_cmd) % 2 == 0
        # Single-quote boundaries in the pgrep call must also be closed (second injection point)
        pgrep_cmd = next((c for c in captured if "pgrep" in c), "")
        assert self._shell_quote_count(pgrep_cmd) % 2 == 0
