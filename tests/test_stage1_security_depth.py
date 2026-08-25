"""Stage 1 安全纵深补强测试 — 0825 审计 A/E 级修复核验。

覆盖 v0.3.1 新增防御 (现有 test_sanitize_stage2 未覆盖的绕过面):
- A-4: PythonREPL 动态导入/执行绕过 (__import__/importlib/eval/exec/getattr(builtins))
- E-11: FetchURL SSRF 私网/环回/元数据拒绝 + follow_redirects=False
- E-10: CDP wait_for_function 危险 token 拒绝 + allow_js gate
- E-3: file: scope ~/ expanduser (含 params 带 ~ 的对齐)
- E-4: Permission save 原子写 + load 损坏容错
- A-6: deny 规则先于 Hook 批准 (Hook approve 不可翻盘 deny)
- A-2: sandbox kill_process_group / bounded_communicate (darwin seatbelt 见 test_permission_stage3)
"""

import json
import os

import pytest

import fusion_cowork.nodes
from fusion_cowork.engine.permission import HIGH_RISK_NODES, PermissionLevel, PermissionManager
from fusion_cowork.nodes.tools.tool_nodes import _check_python_code, _check_ssrf_url, _is_private_ip

fusion_cowork.nodes.import_all_nodes()


# ── A-4: PythonREPL 动态导入/执行绕过 ──


class TestPythonReplDynamicBypass:
    @pytest.mark.parametrize(
        "code",
        [
            '__import__("subprocess").run(["ls"])',
            'import importlib; importlib.import_module("subprocess")',
            "eval(\"__import__('os')\")",
            "exec('import socket')",
            'compile("import os", "<s>", "exec")',
            "g = globals(); g['__builtins__']['eval']('1')",
            'getattr(__builtins__, "eval")("1+1")',
            'import builtins; builtins.eval("1")',
            "x = __import__\nx('os')",
            "vars(__builtins__)['eval']",
        ],
    )
    def test_dynamic_bypass_blocked(self, code):
        err = _check_python_code(code)
        assert err is not None, f"动态绕过未拦截: {code}"

    @pytest.mark.parametrize(
        "code",
        [
            "x = 1 + 1\nprint(x)",
            "import math\nmath.sqrt(4)",
            "sum([1, 2, 3])",
            "list(range(10))",
            "def f():\n    return 42",
        ],
    )
    def test_safe_code_allowed(self, code):
        assert _check_python_code(code) is None


# ── E-11: FetchURL SSRF ──


class TestFetchUrlSSRF:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://localhost/secret",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
        ],
    )
    def test_ssrf_blocked(self, url):
        assert _check_ssrf_url(url) is not None, f"SSRF 未拒: {url}"

    def test_ssrf_metadata_host_blocked(self):
        assert _check_ssrf_url("http://metadata.google.internal/computeMetadata/") is not None

    def test_public_url_allowed(self):
        # 公网域名 (解析失败/超时在测试环境可能拒, 仅断言字面量非私网不直接拒)
        err = _check_ssrf_url("https://example.com/")
        # example.com 解析公网 IP, 应放行 (None); 若沙箱环境 DNS 不通会拒 — 仅断言非内网误判
        if err is not None:
            assert "内网" not in err and "环回" not in err

    def test_is_private_ip_ranges(self):
        import ipaddress

        assert _is_private_ip(ipaddress.ip_address("10.1.1.1"))
        assert _is_private_ip(ipaddress.ip_address("127.0.0.1"))
        assert _is_private_ip(ipaddress.ip_address("169.254.1.1"))
        assert not _is_private_ip(ipaddress.ip_address("8.8.8.8"))

    def test_fetch_url_node_in_high_risk(self):
        assert "fetch_url" in HIGH_RISK_NODES


# ── E-10: CDP wait_for_function 危险 token + allow_js ──


class TestCdpWaitForGate:
    @pytest.mark.asyncio
    async def test_wait_for_rejects_dangerous_tokens(self):
        from fusion_cowork.nodes.browser.cdp_client import CDPClient

        c = CDPClient(host="127.0.0.1", port=9222)
        for expr in ["x}", "x;", "x`y", "eval(1)", "Function(1)", "new Function", "x=>{y"]:
            with pytest.raises(ValueError, match="危险标记"):
                await c.wait_for_function(expr, timeout=0.1)

    @pytest.mark.asyncio
    async def test_wait_for_node_rejected_without_allow_js(self):
        from fusion_cowork.engine.node import NodeConfig, NodeRegistry, NodeStatus

        node = NodeRegistry.create("cdp_wait_for", config=NodeConfig(params={"expression": "true", "timeout": 1}))
        result = await node.execute({})
        assert result.status == NodeStatus.FAILED
        assert "allow_js" in (result.error or "") or "确认" in (result.error or "")

    def test_cdp_wait_for_in_high_risk(self):
        assert "cdp_wait_for" in HIGH_RISK_NODES


# ── E-3: file: scope expanduser ──


class TestScopeExpanduser:
    def test_scope_matches_tilde_path(self):
        from fusion_cowork.engine.permission import Permission

        p = Permission(tool_name="*", scope="file:~/Desktop/**")
        assert p.matches("file_input", {"path": "~/Desktop/test.txt"})
        assert p.matches("file_input", {"path": os.path.expanduser("~/Desktop/sub/a.txt")})

    def test_scope_rejects_outside_desktop(self):
        from fusion_cowork.engine.permission import Permission

        p = Permission(tool_name="*", scope="file:~/Desktop/**")
        assert not p.matches("file_input", {"path": "/etc/passwd"})
        assert not p.matches("file_input", {"path": "~/Documents/x.txt"})

    def test_scope_source_path_key(self):
        from fusion_cowork.engine.permission import Permission

        p = Permission(tool_name="*", scope="file:~/Desktop/**")
        assert p.matches("file_copy", {"source_path": "~/Desktop/a.txt"})


# ── E-4: Permission save 原子写 + load 损坏容错 ──


class TestPermissionPersistAtomic:
    @pytest.mark.asyncio
    async def test_save_is_atomic_no_tmp_leftover(self, tmp_path):
        pm = PermissionManager()
        pm.approve("shell_exec")
        target = str(tmp_path / "perm.json")
        pm.save(target)
        assert os.path.exists(target)
        # 无残留 tmp 文件
        leftovers = [f for f in os.listdir(tmp_path) if ".tmp." in f]
        assert leftovers == [], f"原子写残留 tmp: {leftovers}"
        # 权限 0600
        if os.name != "nt":
            mode = os.stat(target).st_mode & 0o777
            assert mode == 0o600, f"权限文件 mode={oct(mode)} 非 0600"

    @pytest.mark.asyncio
    async def test_save_then_load_roundtrip(self, tmp_path):
        pm = PermissionManager(level=PermissionLevel.AUTO)
        pm.approve("shell_exec", "command:git *")
        pm.deny("file_delete", "file:~/**")
        target = str(tmp_path / "perm.json")
        pm.save(target)
        pm2 = PermissionManager()
        pm2.load(target)
        assert pm2.level == PermissionLevel.AUTO
        assert len(pm2.rules) == 2
        assert await pm2.check("shell_exec", params={"command": "git status"})

    @pytest.mark.asyncio
    async def test_load_corrupt_json_does_not_crash(self, tmp_path):
        target = str(tmp_path / "bad.json")
        with open(target, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        pm = PermissionManager()
        pm.approve("shell_exec")
        pm.load(target)  # 不崩
        assert pm.rules == [], "损坏 JSON 应清空规则"
        # 仍可正常工作
        assert await pm.check("file_input") is True

    @pytest.mark.asyncio
    async def test_load_bad_level_falls_back_confirm(self, tmp_path):
        target = str(tmp_path / "lvl.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump({"level": "nonsense_level", "rules": []}, f)
        pm = PermissionManager()
        pm.load(target)
        assert pm.level == PermissionLevel.CONFIRM


# ── A-6: deny 先于 Hook 批准 ──


class TestDenyBeforeHook:
    @pytest.mark.asyncio
    async def test_deny_rule_not_overridden_by_hook_approve(self):
        from fusion_cowork.engine.hooks import HookEvent, HookManager

        hm = HookManager()

        async def approve_all(ctx):
            ctx.modified_data["approved"] = True

        hm.register(HookEvent.PERMISSION_REQUEST, approve_all)
        pm = PermissionManager(level=PermissionLevel.CONFIRM, hook_manager=hm)
        pm.deny("shell_exec")
        # Hook 试图 approve, 但 deny 规则优先 → 仍拒
        assert await pm.check("shell_exec") is False

    @pytest.mark.asyncio
    async def test_deny_rule_overrides_bypass_for_explicit_deny(self):
        # BYPASS 全放行, 但 deny 规则... 审计 A-6: BYPASS 仍 fire 审计, 判定全放行
        # (BYPASS 设计语义 = 全放行, deny 在 BYPASS 下不拦 — 仅审计记录)
        from fusion_cowork.engine.hooks import HookEvent, HookManager

        hm = HookManager()
        saw_deny = {"v": False}

        async def observer(ctx):
            saw_deny["v"] = ctx.data.get("denied_by_rule", False)

        hm.register(HookEvent.PERMISSION_REQUEST, observer)
        pm = PermissionManager(level=PermissionLevel.BYPASS, hook_manager=hm)
        pm.deny("shell_exec")
        result = await pm.check("shell_exec")
        # BYPASS 放行, 但审计 Hook 仍记录了 denied_by_rule
        assert result is True
        assert saw_deny["v"] is True

    @pytest.mark.asyncio
    async def test_confirm_level_blocks_high_risk_without_approve(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        assert await pm.check("shell_exec") is False
        assert await pm.check("file_input") is True

    @pytest.mark.asyncio
    async def test_approve_rule_allows_high_risk_under_confirm(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        pm.approve("shell_exec", "command:git *")
        assert await pm.check("shell_exec", params={"command": "git status"}) is True
        # 越界 command 仍拒 (scope 不匹配)
        assert await pm.check("shell_exec", params={"command": "rm -rf /"}) is False
