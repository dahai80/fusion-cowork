"""fusion-guard 集成测试 (issue #73)。

纯离线 — 无 live daemon。三策略:
- A: mock GuardClient (evaluate 返预设 verdict) → 验 PermissionManager.check 委托/放行/拒绝/待确认
- B: fake UDS server (asyncio.start_unix_server) → 验 GuardClient wire 帧格式/解析
- C: opt-in OFF 回归 → guard 未启用, check 走原逻辑

清理: monkeypatch.setenv + tmp_path + close_guard_client() autouse。
"""

import asyncio
import json
import os

import pytest

import fusion_cowork.nodes
from fusion_cowork.engine.permission import PermissionManager
from fusion_cowork.security import guard as guard_mod

fusion_cowork.nodes.import_all_nodes()


@pytest.fixture(autouse=True)
def _reset_guard_singleton(monkeypatch, tmp_path):
    # 每测隔离: 关 guard 单例 + 重定向缓存目录到 tmp_path
    monkeypatch.setattr(guard_mod, "_guard_client", None)
    monkeypatch.setattr(guard_mod, "RULES_CACHE_DIR", str(tmp_path / ".fusion-guard"))
    monkeypatch.setattr(guard_mod, "RULES_CACHE_FILE", str(tmp_path / ".fusion-guard" / "rules-cache.json"))
    yield
    # 末尾清全局单例 (防泄漏到下一测)
    guard_mod._guard_client = None


# ── 策略 A: mock GuardClient ──


class FakeGuardClient:
    def __init__(self, verdict=None, rules=None, confirm_ok=True):
        self._verdict = verdict
        self._rules = rules
        self._confirm_ok = confirm_ok
        self.evaluate_calls = 0
        self.confirm_calls = 0

    async def evaluate(self, **kwargs):
        self.evaluate_calls += 1
        return self._verdict

    async def confirm(self, **kwargs):
        self.confirm_calls += 1
        return self._confirm_ok

    async def rules_dump(self):
        if self._rules is None:
            return None
        return self._rules, 42


def _wire_guard(monkeypatch, fake_client):
    monkeypatch.setenv("FUSION_GUARD_ENABLED", "1")
    monkeypatch.setattr(guard_mod, "guard_enabled", lambda: True)
    monkeypatch.setattr(guard_mod, "get_guard_client", lambda: fake_client)
    # permission.py 内 import 的 get_guard_client 也须指到 fake
    import fusion_cowork.engine.permission as perm_mod

    monkeypatch.setattr(perm_mod, "_guard_cached_epoch", lambda: 0)


def _verdict(**kw):
    return guard_mod.GuardVerdict(
        action=kw.get("action", "block"),
        risk_level=kw.get("risk_level", "l4"),
        reason=kw.get("reason", ""),
        stage=kw.get("stage", ""),
        requires_approval=kw.get("requires_approval", False),
        redacted_content=kw.get("redacted_content"),
        seatbelt_required=kw.get("seatbelt_required", False),
        action_id=kw.get("action_id"),
        verdict_epoch=kw.get("verdict_epoch", 0),
        verdict_ttl_secs=kw.get("verdict_ttl_secs", 0),
        inferred_category=kw.get("inferred_category", ""),
        category_hint=kw.get("category_hint"),
    )


# === A 测试占位 — Edit 填 ===


class TestPermissionGuardWiring:
    @pytest.mark.asyncio
    async def test_guard_allow_high_risk(self, monkeypatch):
        fake = FakeGuardClient(verdict=_verdict(action="allow", risk_level="l1"))
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager()
        ok = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok is True
        assert fake.evaluate_calls == 1

    @pytest.mark.asyncio
    async def test_guard_block_denies(self, monkeypatch):
        fake = FakeGuardClient(verdict=_verdict(action="block", risk_level="l4"))
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager()
        ok = await pm.check("shell_exec", "execute", {"command": "rm -rf /"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_guard_l4_denies_even_if_action_not_block(self, monkeypatch):
        fake = FakeGuardClient(verdict=_verdict(action="allow", risk_level="l4"))
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager()
        ok = await pm.check("python_repl", "execute", {"code": "os.system('x')"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_guard_requires_approval_pends(self, monkeypatch):
        fake = FakeGuardClient(
            verdict=_verdict(action="preview", risk_level="l3", requires_approval=True, action_id="aid-1")
        )
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager()
        ok = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok is False
        assert "aid-1" in pm._pending_guard_approvals
        assert pm._pending_guard_approvals["aid-1"] == "shell_exec"

    @pytest.mark.asyncio
    async def test_guard_confirm_approved_adds_local_rule(self, monkeypatch):
        fake = FakeGuardClient(
            verdict=_verdict(action="preview", risk_level="l3", requires_approval=True, action_id="aid-2")
        )
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager()
        await pm.check("shell_exec", "execute", {"command": "ls"})
        ok = await pm.confirm_guard("aid-2", approved=True, approved_by="tester")
        assert ok is True
        assert fake.confirm_calls == 1
        assert "aid-2" not in pm._pending_guard_approvals
        # 本地 approve 规则已加 → 后续 check 不再走 guard (approve 规则命中先返回)
        fake._verdict = _verdict(action="block")
        ok2 = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok2 is True

    @pytest.mark.asyncio
    async def test_guard_confirm_denied_adds_deny_rule(self, monkeypatch):
        fake = FakeGuardClient(
            verdict=_verdict(action="preview", risk_level="l3", requires_approval=True, action_id="aid-3")
        )
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager()
        await pm.check("shell_exec", "execute", {"command": "ls"})
        ok = await pm.confirm_guard("aid-3", approved=False, approved_by="tester")
        assert ok is True
        # deny 规则已加 → 后续 check 即拒
        fake._verdict = _verdict(action="allow", risk_level="l1")
        ok2 = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok2 is False

    @pytest.mark.asyncio
    async def test_guard_unreachable_no_cache_fail_closed(self, monkeypatch):
        fake = FakeGuardClient(verdict=None)  # evaluate 返 None = 不可达
        _wire_guard(monkeypatch, fake)
        monkeypatch.setattr(guard_mod, "load_cached_rules", lambda: None)
        pm = PermissionManager()
        ok = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok is False  # 高风险 fail-closed

    @pytest.mark.asyncio
    async def test_guard_unreachable_cached_deny(self, monkeypatch):
        fake = FakeGuardClient(verdict=None)
        _wire_guard(monkeypatch, fake)
        monkeypatch.setattr(guard_mod, "load_cached_rules", lambda: ([{"tool": "shell_exec", "action": "block"}], 1))
        pm = PermissionManager()
        ok = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_guard_unreachable_cached_allow(self, monkeypatch):
        fake = FakeGuardClient(verdict=None)
        _wire_guard(monkeypatch, fake)
        monkeypatch.setattr(guard_mod, "load_cached_rules", lambda: ([{"tool": "shell_exec", "action": "allow"}], 1))
        pm = PermissionManager()
        ok = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok is True

    @pytest.mark.asyncio
    async def test_low_risk_node_skips_guard(self, monkeypatch):
        fake = FakeGuardClient(verdict=_verdict(action="block"))
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager()
        ok = await pm.check("file_input", "execute", {"path": "/tmp/x"})
        assert ok is True
        assert fake.evaluate_calls == 0  # 低风险不调 guard

    @pytest.mark.asyncio
    async def test_explicit_approve_rule_skips_guard(self, monkeypatch):
        fake = FakeGuardClient(verdict=_verdict(action="block"))
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager()
        pm.approve("shell_exec")
        ok = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok is True
        assert fake.evaluate_calls == 0  # approve 规则命中先返回, 不走 guard

    @pytest.mark.asyncio
    async def test_preview_redact_denies(self, monkeypatch):
        # redact+l3 (非 l1/l2 allow 分支, 无 requires_approval) → 保守拒绝路径
        fake = FakeGuardClient(verdict=_verdict(action="redact", risk_level="l3", requires_approval=False))
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager()
        ok = await pm.check("fetch_url", "execute", {"url": "http://x"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_bypass_level_skips_guard(self, monkeypatch):
        from fusion_cowork.engine.permission import PermissionLevel

        fake = FakeGuardClient(verdict=_verdict(action="block"))
        _wire_guard(monkeypatch, fake)
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        ok = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok is True
        assert fake.evaluate_calls == 0


class TestNodeToGuardContent:
    def test_shell_exec(self):
        content, ctype = guard_mod.node_to_guard_content("shell_exec", {"command": "ls -la"})
        assert content == "ls -la"
        assert ctype == "shell"

    def test_python_repl(self):
        content, ctype = guard_mod.node_to_guard_content("python_repl", {"code": "print(1)"})
        assert content == "print(1)"
        assert ctype == "code"

    def test_cdp_evaluate(self):
        content, ctype = guard_mod.node_to_guard_content("cdp_evaluate", {"script": "document.title"})
        assert content == "document.title"
        assert ctype == "code"

    def test_fetch_url(self):
        content, ctype = guard_mod.node_to_guard_content("fetch_url", {"url": "http://x"})
        assert content == "http://x"
        assert ctype == "text"

    def test_file_delete(self):
        content, ctype = guard_mod.node_to_guard_content("file_delete", {"path": "/tmp/x"})
        assert content == "/tmp/x"
        assert ctype == "text"

    def test_unknown_node_fallback_json(self):
        content, ctype = guard_mod.node_to_guard_content("weird_node", {"a": 1, "b": 2})
        assert "weird_node" in content
        assert ctype == "json"

    def test_known_node_missing_key(self):
        content, ctype = guard_mod.node_to_guard_content("shell_exec", {})
        assert content == "shell_exec"
        assert ctype == "shell"


# ── 策略 B: fake UDS server ──


# === B 测试占位 — Edit 填 ===


def _short_sock(tmp_path):
    # macOS AF_UNIX 路径上限 ~104 字节, tmp_path 嵌套深 → 用 /tmp 短路径
    path = f"/tmp/fcguard-{os.getpid()}-{id(tmp_path)}.sock"
    if os.path.exists(path):
        os.unlink(path)
    return path


async def _start_fake_guard(sock_path, response_factory):
    # 伪造 fusion-guard daemon: 读换行分隔 JSON-RPC 请求, 回原始 JSON (无尾换行)
    async def handle(reader, writer):
        try:
            line = await reader.readuntil(b"\n")
            req = json.loads(line.decode("utf-8"))
            resp = response_factory(req)
            writer.write(json.dumps(resp).encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    server = await asyncio.start_unix_server(handle, path=sock_path)
    return server


class TestGuardClientWire:
    @pytest.mark.asyncio
    async def test_ping_success(self, tmp_path, monkeypatch):
        sock = _short_sock(tmp_path)

        def factory(req):
            assert req["method"] == "guard.ping"
            return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}

        server = await _start_fake_guard(sock, factory)
        try:
            client = guard_mod.GuardClient(sock_path=sock, secret=None)
            assert await client.ping() is True
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_ping_connect_fail(self, tmp_path):
        client = guard_mod.GuardClient(sock_path=str(tmp_path / "nope.sock"))
        assert await client.ping() is False

    @pytest.mark.asyncio
    async def test_evaluate_returns_verdict(self, tmp_path):
        sock = _short_sock(tmp_path)

        def factory(req):
            assert req["method"] == "guard.evaluate"
            assert req["params"]["content"] == "ls"
            assert req["params"]["content_type"] == "shell"
            verdict = {
                "action": "allow",
                "risk_level": "l1",
                "reason": "ok",
                "stage": "static",
                "requires_approval": False,
                "action_id": None,
                "verdict_epoch": 7,
            }
            return {"jsonrpc": "2.0", "id": req["id"], "result": verdict}

        server = await _start_fake_guard(sock, factory)
        try:
            client = guard_mod.GuardClient(sock_path=sock)
            v = await client.evaluate(
                content="ls", content_type="shell", tenant_id="t1", requester="u1", action="shell_exec"
            )
            assert v is not None
            assert v.action == "allow"
            assert v.risk_level == "l1"
            assert v.verdict_epoch == 7
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_evaluate_error_returns_none(self, tmp_path):
        sock = _short_sock(tmp_path)

        def factory(req):
            return {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32001, "message": "unauthorized"}}

        server = await _start_fake_guard(sock, factory)
        try:
            client = guard_mod.GuardClient(sock_path=sock)
            v = await client.evaluate(content="ls", content_type="shell")
            assert v is None
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_confirm(self, tmp_path):
        sock = _short_sock(tmp_path)
        seen = {}

        def factory(req):
            seen["params"] = req["params"]
            return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}

        server = await _start_fake_guard(sock, factory)
        try:
            client = guard_mod.GuardClient(sock_path=sock)
            ok = await client.confirm(action_id="aid", approved=True, approved_by="u", tenant_id="t")
            assert ok is True
            assert seen["params"]["approved"] is True
            assert seen["params"]["action_id"] == "aid"
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_rules_dump(self, tmp_path):
        sock = _short_sock(tmp_path)

        def factory(req):
            return {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"rules": [{"tool": "shell_exec", "action": "block"}], "epoch": 99},
            }

        server = await _start_fake_guard(sock, factory)
        try:
            client = guard_mod.GuardClient(sock_path=sock)
            dumped = await client.rules_dump()
            assert dumped is not None
            rules, epoch = dumped
            assert epoch == 99
            assert rules[0]["tool"] == "shell_exec"
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_secret_injected_in_params(self, tmp_path):
        sock = _short_sock(tmp_path)
        seen = {}

        def factory(req):
            seen["secret"] = req["params"].get("secret")
            return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}

        server = await _start_fake_guard(sock, factory)
        try:
            client = guard_mod.GuardClient(sock_path=sock, secret="s3cr3t")
            await client.evaluate(content="ls", content_type="shell")
            assert seen["secret"] == "s3cr3t"
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_ping_skips_secret(self, tmp_path):
        sock = _short_sock(tmp_path)
        seen = {}

        def factory(req):
            seen["secret"] = req["params"].get("secret")
            return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}

        server = await _start_fake_guard(sock, factory)
        try:
            client = guard_mod.GuardClient(sock_path=sock, secret="s3cr3t")
            await client.ping()
            assert "secret" not in seen or seen["secret"] is None
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_round_trip_id_increments(self, tmp_path):
        sock = _short_sock(tmp_path)
        ids = []

        def factory(req):
            ids.append(req["id"])
            return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}

        server = await _start_fake_guard(sock, factory)
        try:
            client = guard_mod.GuardClient(sock_path=sock)
            await client.ping()
            # 服务器单连接 handler 回完即关 → 手动 close 强制下轮重连, 验 id 自增
            await client.close()
            await client.ping()
            assert ids == [1, 2]
            await client.close()
        finally:
            server.close()
            await server.wait_closed()


# ── 策略 C: opt-in OFF 回归 ──


# === C 测试占位 — Edit 填 ===


class TestGuardOptInOff:
    def test_guard_enabled_false_no_env(self, monkeypatch):
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        assert guard_mod.guard_enabled() is False

    def test_guard_enabled_false_env_set_no_socket(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_GUARD_ENABLED", "1")
        monkeypatch.setattr(guard_mod, "DEFAULT_SOCK", str(tmp_path / "nope.sock"))
        assert guard_mod.guard_enabled() is False

    def test_guard_enabled_true(self, monkeypatch, tmp_path):
        sock = tmp_path / "guard.sock"
        sock.touch()
        monkeypatch.setenv("FUSION_GUARD_ENABLED", "1")
        monkeypatch.setattr(guard_mod, "DEFAULT_SOCK", str(sock))
        assert guard_mod.guard_enabled() is True

    def test_get_guard_client_none_when_disabled(self, monkeypatch):
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        assert guard_mod.get_guard_client() is None

    @pytest.mark.asyncio
    async def test_check_legacy_high_risk_denies_without_guard(self, monkeypatch):
        # 无 env → guard 未启用 → 高风险无批准 → 拒 (原行为不变)
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        pm = PermissionManager()
        ok = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_check_legacy_low_risk_allowed_without_guard(self, monkeypatch):
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        pm = PermissionManager()
        ok = await pm.check("file_input", "execute", {"path": "/tmp/x"})
        assert ok is True

    @pytest.mark.asyncio
    async def test_check_legacy_explicit_approve_still_works(self, monkeypatch):
        monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
        pm = PermissionManager()
        pm.approve("shell_exec")
        ok = await pm.check("shell_exec", "execute", {"command": "ls"})
        assert ok is True


class TestGuardCache:
    def test_save_load_cached_rules_roundtrip(self, monkeypatch, tmp_path):
        cache_file = tmp_path / "rc.json"
        monkeypatch.setattr(guard_mod, "RULES_CACHE_FILE", str(cache_file))
        monkeypatch.setattr(guard_mod, "RULES_CACHE_DIR", str(tmp_path))
        guard_mod.save_cached_rules([{"tool": "shell_exec", "action": "block"}], 55)
        loaded = guard_mod.load_cached_rules()
        assert loaded is not None
        rules, epoch = loaded
        assert epoch == 55
        assert rules[0]["action"] == "block"
        assert guard_mod.get_cached_epoch() == 55

    def test_load_missing_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(guard_mod, "RULES_CACHE_FILE", str(tmp_path / "missing.json"))
        assert guard_mod.load_cached_rules() is None
        assert guard_mod.get_cached_epoch() == 0

    def test_load_corrupt_returns_none(self, monkeypatch, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(guard_mod, "RULES_CACHE_FILE", str(bad))
        assert guard_mod.load_cached_rules() is None
