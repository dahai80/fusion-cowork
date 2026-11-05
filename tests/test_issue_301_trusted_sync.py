"""issue #301: deterministic template sync nodes skip per-deployment confirms.

Fresh governance-template deployments used to hit a permission confirm per
http_request sync node (n1b/n4/n6b) before the flow worked. Fix: PermissionManager
seeds narrow allow rules (http_request + url: prefix) for trusted sync targets —
the documented agent-studio approvals endpoint by default, overridable via
FUSION_COWORK_TRUSTED_SYNC_URLS ("off" disables). Deny rules still win (A-6);
approval_gate stays gated (gate decisions are the point).
"""

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.permission_manager import DEFAULT_TRUSTED_SYNC_URLS, trusted_sync_urls

APPROVALS_URL = "http://127.0.0.1:11442/v1/agent-platform/approvals"


@pytest.fixture(autouse=True)
def _clean_guard_env(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_TRUSTED_SYNC_URLS", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)


class TestTrustedSyncSeeding:
    def test_default_seed_allows_documented_approvals_endpoint(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        import asyncio

        ok = asyncio.run(pm.check("http_request", "execute", {"url": APPROVALS_URL, "method": "POST"}))
        assert ok is True
        assert pm.list_pending() == [] or all("http_request" not in str(p) for p in pm.list_pending())

    def test_other_urls_still_gated(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        import asyncio

        ok = asyncio.run(pm.check("http_request", "execute", {"url": "http://10.0.0.5/api"}))
        assert ok is False

    def test_no_blanket_allow_for_http_request(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        import asyncio

        ok = asyncio.run(pm.check("http_request", "execute", {"url": "http://127.0.0.1:11442/other"}))
        assert ok is False

    def test_approval_gate_still_gated(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        import asyncio

        ok = asyncio.run(pm.check("approval_gate", "execute", {}))
        assert ok is False

    def test_deny_rule_beats_trusted_seed(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        from fusion_cowork.engine.permission_manager import Permission

        pm.rules.append(Permission(tool_name="http_request", allowed=False, scope=f"url:{APPROVALS_URL}"))
        import asyncio

        ok = asyncio.run(pm.check("http_request", "execute", {"url": f"{APPROVALS_URL}/sync"}))
        assert ok is False

    def test_seed_idempotent(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        before = len(pm.rules)
        pm._seed_trusted_sync_rules()
        assert len(pm.rules) == before


class TestTrustedSyncEnvOverride:
    def test_env_replaces_defaults(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_TRUSTED_SYNC_URLS", "http://127.0.0.1:9999/api")
        assert trusted_sync_urls() == ("http://127.0.0.1:9999/api",)
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        import asyncio

        ok = asyncio.run(pm.check("http_request", "execute", {"url": "http://127.0.0.1:9999/api/x"}))
        assert ok is True
        ok_default = asyncio.run(pm.check("http_request", "execute", {"url": APPROVALS_URL}))
        assert ok_default is False

    def test_env_off_disables_seeding(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_TRUSTED_SYNC_URLS", "off")
        assert trusted_sync_urls() == ()
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        import asyncio

        ok = asyncio.run(pm.check("http_request", "execute", {"url": APPROVALS_URL}))
        assert ok is False

    def test_default_constant_matches_template_default(self):
        # template approvals_url default and the seed must agree
        assert DEFAULT_TRUSTED_SYNC_URLS == ("http://127.0.0.1:11442/v1/agent-platform/approvals",)


class TestLifecycleReseed:
    def test_load_reseeds_after_replacing_rules(self, tmp_path):
        import json

        rules_file = tmp_path / "permissions.json"
        rules_file.write_text(json.dumps({"level": "confirm", "rules": []}), encoding="utf-8")
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        pm.load(str(rules_file))
        scopes = [r.scope for r in pm.rules if r.tool_name == "http_request" and r.allowed]
        assert f"url:{APPROVALS_URL}" in scopes

    def test_reset_reseeds_baseline(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        pm.reset()
        scopes = [r.scope for r in pm.rules if r.tool_name == "http_request" and r.allowed]
        assert f"url:{APPROVALS_URL}" in scopes


class TestGovernanceTemplateIntegration:
    @pytest.mark.asyncio
    async def test_sync_nodes_pass_gates_stay(self):
        from fusion_cowork.nodes import import_all_nodes
        from fusion_cowork.templates.template_manager import TemplateManager

        import_all_nodes()
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        wf = TemplateManager().template_to_workflow(
            "governance_issue_lifecycle", param_values={"repo": "org/x", "issue_number": 1}
        )
        for nid in ("n1b", "n4", "n6b"):
            params = wf.nodes[nid].config.params
            check_input = dict(params)
            ok = await pm.check("http_request", "execute", check_input)
            assert ok is True, f"{nid} sync should skip confirm"
        gate = await pm.check("approval_gate", "execute", dict(wf.nodes["n5"].config.params))
        assert gate is False
