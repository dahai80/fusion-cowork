"""issue #301: deterministic template sync nodes skip per-deployment confirms.

Fresh governance-template deployments used to hit a permission confirm per
http_request sync node (n1b/n4/n6b) before the flow worked. Fix: check()
consults a trusted-sync URL list directly (NOT seeded rule objects — persisted
rule listings/roundtrips stay pristine): the documented agent-studio approvals
endpoint by default, overridable via FUSION_COWORK_TRUSTED_SYNC_URLS ("off"
disables). Deny rules still win (A-6); approval_gate stays gated.
"""

import asyncio
import json

import pytest

from fusion_cowork.engine import PermissionLevel, PermissionManager
from fusion_cowork.engine.permission_manager import DEFAULT_TRUSTED_SYNC_URLS, Permission, trusted_sync_urls

APPROVALS_URL = "http://127.0.0.1:11442/v1/agent-platform/approvals"


@pytest.fixture(autouse=True)
def _clean_guard_env(monkeypatch):
    monkeypatch.delenv("FUSION_GUARD_ENABLED", raising=False)
    monkeypatch.delenv("FUSION_COWORK_TRUSTED_SYNC_URLS", raising=False)
    monkeypatch.delenv("FUSION_COWORK_HITL_PAUSE", raising=False)


class TestTrustedSyncTargets:
    def test_documented_approvals_endpoint_allowed(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        assert asyncio.run(pm.check("http_request", "execute", {"url": APPROVALS_URL, "method": "POST"})) is True

    def test_other_urls_still_gated(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        assert asyncio.run(pm.check("http_request", "execute", {"url": "http://10.0.0.5/api"})) is False

    def test_no_blanket_allow_for_http_request(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        assert asyncio.run(pm.check("http_request", "execute", {"url": "http://127.0.0.1:11442/other"})) is False

    def test_approval_gate_still_gated(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        assert asyncio.run(pm.check("approval_gate", "execute", {})) is False

    def test_deny_rule_beats_trusted_target(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        pm.rules.append(Permission(tool_name="http_request", allowed=False, scope=f"url:{APPROVALS_URL}"))
        ok = asyncio.run(pm.check("http_request", "execute", {"url": f"{APPROVALS_URL}/sync"}))
        assert ok is False

    def test_rules_list_untouched_by_trusted_targets(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        asyncio.run(pm.check("http_request", "execute", {"url": APPROVALS_URL}))
        assert pm.rules == []

    def test_missing_url_param_not_trusted(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        assert asyncio.run(pm.check("http_request", "execute", {"method": "POST"})) is False


class TestTrustedSyncEnvOverride:
    def test_env_replaces_defaults(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_TRUSTED_SYNC_URLS", "http://127.0.0.1:9999/api")
        assert trusted_sync_urls() == ("http://127.0.0.1:9999/api",)
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        ok = asyncio.run(pm.check("http_request", "execute", {"url": "http://127.0.0.1:9999/api/x"}))
        assert ok is True
        ok_default = asyncio.run(pm.check("http_request", "execute", {"url": APPROVALS_URL}))
        assert ok_default is False

    def test_env_off_disables_trusting(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_TRUSTED_SYNC_URLS", "off")
        assert trusted_sync_urls() == ()
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        assert asyncio.run(pm.check("http_request", "execute", {"url": APPROVALS_URL})) is False

    def test_default_constant_matches_template_default(self):
        # template approvals_url default and the trusted list must agree
        assert DEFAULT_TRUSTED_SYNC_URLS == ("http://127.0.0.1:11442/v1/agent-platform/approvals",)


class TestLifecycleBaseline:
    def test_trusted_target_survives_load(self, tmp_path):
        rules_file = tmp_path / "permissions.json"
        rules_file.write_text(json.dumps({"level": "confirm", "rules": []}), encoding="utf-8")
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        pm.load(str(rules_file))
        assert asyncio.run(pm.check("http_request", "execute", {"url": APPROVALS_URL})) is True

    def test_trusted_target_survives_reset(self):
        pm = PermissionManager(level=PermissionLevel.CONFIRM)
        pm.reset()
        assert asyncio.run(pm.check("http_request", "execute", {"url": APPROVALS_URL})) is True


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
