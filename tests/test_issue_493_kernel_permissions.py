"""issue #493: PermissionManager rule-model/scope-matching delegated to fusion-harness
scoped_permissions (K1 kernel migration).

Boundary (kernel scoped_permissions docstring: hook audit / HITL gate exemption /
trusted-sync / capability gate / guard IPC stay consumer-side; engine.check() not
delegated because it folds bypass/allow-rule/default-allow into one "allow" verdict
while cowork check() needs the tri-state for CR-16 ordering):

- Permission.matches delegates to kernel _scope_match for file:/command:/url: scopes
- parity overrides preserved: no params → match any scope; params-without-key → no
  match (kernel empty-resource match NOT copied); file: expanduser both sides (E-3);
  unknown prefix scope → True fallback
- kernel fix adopted: path boundary check (file:/a no longer matches file:/ab —
  upstream raw-startswith quirk intentionally not copied)
- on-disk compat: existing permissions.json fixture loads via load() unchanged;
  approval journal pending entries restore via restore_pending_details (#463 format)
- shim re-exports kernel engine types
"""

from __future__ import annotations

import json

from fusion_cowork.engine.permission_manager import (
    HIGH_RISK_NODES,
    HITL_GATE_TOOLS,
    Permission,
    PermissionLevel,
    PermissionManager,
)


class TestKernelScopeMatching:
    def test_wildcard_and_subject(self):
        assert Permission(tool_name="*", scope="*").matches("anything", {"x": 1})
        assert Permission(tool_name="shell_exec", scope="*").matches("shell_exec")
        assert not Permission(tool_name="shell_exec", scope="*").matches("file_read")

    def test_file_scope_expanduser_both_sides(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        rule = Permission(tool_name="file_write", allowed=True, scope="file:~/Desktop/**")
        assert rule.matches("file_write", {"path": str(home / "Desktop" / "a.txt")})
        assert not rule.matches("file_write", {"path": str(home / "Documents" / "a.txt")})
        # E-3: ~ on the value side is expanded too
        assert rule.matches("file_write", {"path": "~/Desktop/b.txt"})

    def test_file_scope_path_boundary_kernel_fix(self, tmp_path, monkeypatch):
        # kernel fix: file:/data no longer wrongly matches /database (upstream raw startswith quirk)
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        rule = Permission(tool_name="file_read", scope="file:~/data")
        assert rule.matches("file_read", {"path": str(home / "data")})
        assert rule.matches("file_read", {"path": str(home / "data" / "f.txt")})
        assert not rule.matches("file_read", {"path": str(home / "database")})

    def test_file_scope_alt_keys(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        rule = Permission(tool_name="file_output", scope="file:~/out/**")
        for key in ("output_path", "save_path", "source_path"):
            assert rule.matches("file_output", {key: str(home / "out" / "x.bin")}), key

    def test_command_scope(self):
        rule = Permission(tool_name="shell_exec", scope="command:git *")
        assert rule.matches("shell_exec", {"command": "git status"})
        assert not rule.matches("shell_exec", {"command": "rm -rf /"})

    def test_url_scope(self):
        rule = Permission(tool_name="mcp_client", scope="url:http://127.0.0.1:9100/**")
        assert rule.matches("mcp_client", {"url": "http://127.0.0.1:9100/tools"})
        assert not rule.matches("mcp_client", {"url": "http://127.0.0.1:9101/tools"})

    def test_no_params_matches_any_scope_parity(self):
        # Upstream and kernel agree: calls without params match any scope (deny rules are scanned first
        # in check(); the semantics are protected by check()'s gate exemption — the matching layer keeps the original semantics)
        rule = Permission(tool_name="shell_exec", allowed=False, scope="file:~/x/**")
        assert rule.matches("shell_exec", None)
        assert rule.matches("shell_exec", {})

    def test_params_without_key_no_match_parity(self):
        # parity: params present but without command/url key → no match (kernel's empty-resource match NOT copied)
        assert not Permission(tool_name="shell_exec", scope="command:git").matches("shell_exec", {"cwd": "/tmp"})
        assert not Permission(tool_name="mcp_client", scope="url:http://x/**").matches("mcp_client", {"timeout": 1})

    def test_unknown_prefix_scope_fallback_true_parity(self):
        # parity: unknown prefix scope → True (kernel's resource-prefix match NOT copied)
        assert Permission(tool_name="shell_exec", scope="weird:prefix").matches("shell_exec", {"anything": 1})


class TestOnDiskCompat:
    def test_permissions_json_fixture_loads_unchanged(self, tmp_path):
        # Production on-disk format (level + rules[tool_name/allowed/scope]) loads and works via the new matching layer unchanged
        fixture = {
            "level": "confirm",
            "rules": [
                {"tool_name": "shell_exec", "allowed": True, "scope": "command:git *"},
                {"tool_name": "http_request", "allowed": False, "scope": "url:http://evil.example/**"},
            ],
        }
        path = tmp_path / "permissions.json"
        path.write_text(json.dumps(fixture), encoding="utf-8")
        pm = PermissionManager()
        pm.load(str(path))
        assert pm.level == PermissionLevel.CONFIRM
        assert len(pm.rules) == 2
        # deny rules still hit through the kernel matching layer
        import asyncio

        assert asyncio.run(pm.check("http_request", params={"url": "http://evil.example/api"})) is False
        assert asyncio.run(pm.check("shell_exec", params={"command": "git log"})) is True

    def test_save_roundtrip_same_format(self, tmp_path):
        pm = PermissionManager()
        pm.approve("shell_exec", scope="command:git *")
        path = tmp_path / "out.json"
        pm.save(str(path))
        data = json.loads(path.read_text())
        assert data["level"] == "confirm"
        assert data["rules"] == [{"tool_name": "shell_exec", "allowed": True, "scope": "command:git *"}]
        pm2 = PermissionManager()
        pm2.load(str(path))
        assert pm2.rules[0].matches("shell_exec", {"command": "git push"})

    def test_journal_pending_entries_restore_via_engine_path(self):
        # #463 approval-journal pending rows (production on-disk shape) rebuilt via restore_pending_details,
        # confirm route consumes them as usual — persisted entries readable after the queue switch
        pm = PermissionManager()
        restored = pm.restore_pending_details(
            [
                {"action_id": "local_ab12cd34", "tool_name": "shell_exec", "run_id": "run_x", "ts": 1000.0},
                {"action_id": "local_ef56ab78", "tool_name": "deploy_service", "run_id": "run_y", "ts": 1001.0},
            ]
        )
        assert restored == 2
        pending = pm.list_pending()
        assert [p["action_id"] for p in pending] == ["local_ab12cd34", "local_ef56ab78"]
        details = pm.get_pending_details("local_ab12cd34")
        assert details["execution_id"] == "run_x"
        assert details["restored"] is True
        assert pm.find_pending_by_execution("run_y")["tool_name"] == "deploy_service"
        # confirm approve → lands an approve rule (gate tools excepted — #340)
        import asyncio

        assert asyncio.run(pm.confirm_guard("local_ab12cd34", True, approved_by="op")) is True
        assert any(r.tool_name == "shell_exec" and r.allowed for r in pm.rules)
        assert pm.discard_pending_by_execution("run_y") == ["local_ef56ab78"]


class TestConsumerWiringUnchanged:
    def test_hitl_gate_tool_exempt_from_deny_rules(self):
        # #340: gates do not consume deny rules — consumer wiring preserved after the kernel migration.
        # Whether deny rules exist does not change the gate check result (gates take the high-risk HITL path;
        # deny-rule scanning is exempted — behavior matches a rule-less manager)
        import asyncio

        pm = PermissionManager()
        pm.deny("approval_gate")
        pm_clean = PermissionManager()
        assert asyncio.run(pm.check("approval_gate")) == asyncio.run(pm_clean.check("approval_gate"))
        # deny rules on non-gate tools still hit as usual
        pm.deny("shell_exec")
        assert asyncio.run(pm.check("shell_exec", params={"command": "ls"})) is False

    def test_high_risk_set_still_cowork_authoritative(self):
        # cowork's HIGH_RISK_NODES is the authoritative set (the kernel's built-in subset does not cover new nodes)
        for node in ("deploy_service", "team_call", "coordinator_fanout", "claude_dispatch"):
            assert node in HIGH_RISK_NODES
        assert "approval_gate" in HIGH_RISK_NODES
        assert isinstance(HITL_GATE_TOOLS, set)

    def test_bypass_overrides_deny_rule_order_unchanged(self):
        # Order unchanged: the BYPASS verdict returns before the deny result (check() current order:
        # deny scan → hook → BYPASS → denied) — the kernel migration does not change the CR-16/A-6 order
        pm = PermissionManager(level=PermissionLevel.BYPASS)
        pm.deny("shell_exec")
        pm_confirm = PermissionManager()
        pm_confirm.deny("shell_exec")
        import asyncio

        assert asyncio.run(pm.check("shell_exec", params={"command": "ls"})) is True
        assert asyncio.run(pm_confirm.check("shell_exec", params={"command": "ls"})) is False


def test_shim_reexports_kernel_engine():
    from fusion_cowork.engine import permission as perm_shim

    assert perm_shim.KernelScopedPermissionEngine is not None
    assert perm_shim.KERNEL_DECISIONS == ("allow", "deny", "ask")
    # The kernel matching function is exactly what Permission.matches delegates to
    from fusion_harness.scoped_permissions import _scope_match

    assert _scope_match("command:git", "git status")
