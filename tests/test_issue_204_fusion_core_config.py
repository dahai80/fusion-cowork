import sys

import pytest


def _clear_key_env(monkeypatch):
    for var in ("FUSION_CLIENT_KEY", "FUSION_MLX_API_KEY", "MLX_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestClientKeyResolution:
    def test_canonical_env_used(self, monkeypatch):
        from fusion_cowork.ai.mlx_client import FusionMLXClient

        _clear_key_env(monkeypatch)
        monkeypatch.setenv("FUSION_CLIENT_KEY", "ck-canonical")
        assert FusionMLXClient().api_key == "ck-canonical"

    def test_legacy_alias_still_resolved_via_fusion_core(self, monkeypatch):
        # the legacy alias chain is consolidated inside fusion-core (client_key); this repo no longer hand-writes it
        pytest.importorskip("fusion_core")
        from fusion_cowork.ai.mlx_client import FusionMLXClient

        _clear_key_env(monkeypatch)
        monkeypatch.setenv("FUSION_MLX_API_KEY", "ck-legacy")
        assert FusionMLXClient().api_key == "ck-legacy"

    def test_explicit_param_wins_over_env(self, monkeypatch):
        from fusion_cowork.ai.mlx_client import FusionMLXClient

        monkeypatch.setenv("FUSION_CLIENT_KEY", "env-key")
        assert FusionMLXClient(api_key="explicit").api_key == "explicit"

    def test_missing_key_empty_in_dev(self, monkeypatch):
        _clear_key_env(monkeypatch)
        monkeypatch.delenv("FUSION_PROFILE", raising=False)
        from fusion_cowork.ai.mlx_client import FusionMLXClient

        assert FusionMLXClient().api_key == ""

    def test_missing_key_raises_in_prod(self, monkeypatch):
        pytest.importorskip("fusion_core")
        from fusion_cowork.ai.mlx_client import FusionMLXClient

        _clear_key_env(monkeypatch)
        monkeypatch.setenv("FUSION_PROFILE", "prod")
        with pytest.raises(RuntimeError):
            FusionMLXClient()

    def test_fallback_without_fusion_core_reads_canonical_only(self, monkeypatch):
        # bare install (no fusion-core): the fallback path only recognizes the canonical FUSION_CLIENT_KEY
        monkeypatch.setitem(sys.modules, "fusion_core.config", None)
        from fusion_cowork.ai.mlx_client import _client_api_key

        _clear_key_env(monkeypatch)
        monkeypatch.setenv("FUSION_CLIENT_KEY", "fb-key")
        assert _client_api_key() == "fb-key"


class TestShellRegistryAdminKey:
    def test_registration_enabled_with_canonical_admin_key(self, monkeypatch):
        from fusion_cowork.server import shell_registry

        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:11432")
        monkeypatch.delenv("FUSION_ADMIN_KEY", raising=False)
        monkeypatch.delenv("FUSION_GATEWAY_ADMIN_KEY", raising=False)
        monkeypatch.setenv("FUSION_ADMIN_KEY", "adm-canonical")
        assert shell_registry.shell_registration_enabled() is True

    def test_url_missing_short_circuits_before_admin_key_resolution(self, monkeypatch):
        # no admin key resolution triggered when the URL is unconfigured — a missing key in prod is not an error either
        from fusion_cowork.server import shell_registry

        monkeypatch.delenv("FUSION_SHELL_GATEWAY_URL", raising=False)
        monkeypatch.delenv("FUSION_ADMIN_KEY", raising=False)
        monkeypatch.setenv("FUSION_PROFILE", "prod")
        assert shell_registry.shell_registration_enabled() is False

    def test_legacy_admin_key_name_not_read(self, monkeypatch):
        # FUSION_GATEWAY_ADMIN_KEY is contract-external (config-conventions §6);
        # after consolidation no path in this repo reads it anymore
        from fusion_cowork.server import shell_registry

        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:11432")
        monkeypatch.delenv("FUSION_ADMIN_KEY", raising=False)
        monkeypatch.setenv("FUSION_GATEWAY_ADMIN_KEY", "legacy-adm")
        monkeypatch.delenv("FUSION_PROFILE", raising=False)
        assert shell_registry.shell_registration_enabled() is False

    def test_prod_missing_admin_key_raises_when_url_set(self, monkeypatch):
        pytest.importorskip("fusion_core")
        from fusion_cowork.server import shell_registry

        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:11432")
        monkeypatch.delenv("FUSION_ADMIN_KEY", raising=False)
        monkeypatch.setenv("FUSION_PROFILE", "prod")
        with pytest.raises(RuntimeError):
            shell_registry.shell_registration_enabled()

    def test_fallback_without_fusion_core_reads_canonical_only(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "fusion_core.config", None)
        from fusion_cowork.server.shell_registry import _gateway_admin_key

        monkeypatch.delenv("FUSION_ADMIN_KEY", raising=False)
        monkeypatch.setenv("FUSION_ADMIN_KEY", "fb-adm")
        assert _gateway_admin_key() == "fb-adm"


class TestLegacyRefsGone:
    def test_no_local_resolver_defs(self):
        # acceptance: resolution logic lives only in fusion-core; this repo must not define _resolve*key again
        import subprocess

        out = subprocess.run(
            ["grep", "-rn", "def _resolve.*key", "fusion_cowork/"],
            capture_output=True,
            text=True,
        )
        assert out.stdout.strip() == "", f"local resolver defs remain: {out.stdout}"
