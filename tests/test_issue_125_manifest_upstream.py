"""#125: web/manifest.json carries upstream/api_key — gateway #232 dynamic proxy can parse it.

Contract:
- manifest.json declares upstream (11461) + api_key_env;
- load_manifest() injects api_key (env set) / strips it (env empty);
- FUSION_COWORK_WEB_UPSTREAM overrides deployment-specific ports;
- default upstream is non-empty (UpstreamFor can resolve).
"""

from __future__ import annotations

from fusion_cowork.server import shell_registry


class TestManifestUpstream:
    def test_manifest_file_declares_upstream(self):
        manifest = shell_registry.load_manifest()
        assert manifest["upstream"] == "http://127.0.0.1:11461"
        assert manifest["api_key_env"] == "FUSION_COWORK_WEB_API_KEY"

    def test_load_injects_api_key_when_env_set(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "sk-web-1")
        manifest = shell_registry.load_manifest()
        assert manifest["api_key"] == "sk-web-1"

    def test_load_drops_api_key_when_env_empty(self, monkeypatch):
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        manifest = shell_registry.load_manifest()
        assert "api_key" not in manifest

    def test_upstream_env_override(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_WEB_UPSTREAM", "http://0.0.0.0:21461")
        monkeypatch.delenv("FUSION_COWORK_WEB_API_KEY", raising=False)
        manifest = shell_registry.load_manifest()
        assert manifest["upstream"] == "http://0.0.0.0:21461"
