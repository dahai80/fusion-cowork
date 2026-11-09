"""issue #416/#418: intake 加固 — scaffold 双前缀杜绝 + repo owner 默认 org。

- #416: repo_scaffold slug 已带 ai- 前缀时不再重复 (intake slug 形态), 目录/包名/标题归一
- #418: owner decision 2026-11-09 — ai-* 仓默认归属 dahai80-business org,
  FUSION_BUSINESS_GITHUB_OWNER 覆盖
"""

from __future__ import annotations

import httpx

from fusion_cowork.nodes.ops.ops_nodes import RepoScaffoldNode
from fusion_cowork.server.intake import RepoClient, business_github_owner, prepare_intake


def _scaffold(slug: str, base_dir: str) -> RepoScaffoldNode:
    node = RepoScaffoldNode()
    node.config.params = {"slug": slug, "base_dir": base_dir}
    return node


class TestScaffoldPrefix:
    async def test_prefixed_slug_no_double_prefix(self, tmp_path):
        # issue #416: intake 派生 slug 本就带 ai- — 目录必须恰好一层前缀
        res = await _scaffold("ai-acceptance-probe-2026-11-2", str(tmp_path)).execute({})
        assert res.status.value == "success", res.error
        assert res.data["repo_path"] == str(tmp_path / "ai-acceptance-probe-2026-11-2")
        text = (tmp_path / "ai-acceptance-probe-2026-11-2" / "port_registry_entry.json").read_text()
        assert "ai-acceptance-probe-2026-11-2" in text
        assert "ai-ai-" not in text

    async def test_bare_slug_unchanged(self, tmp_path):
        res = await _scaffold("probe", str(tmp_path)).execute({})
        assert res.status.value == "success"
        assert res.data["repo_path"] == str(tmp_path / "ai-probe")

    async def test_pkg_and_title_use_normalized_slug(self, tmp_path):
        res = await _scaffold("ai-pet-hotel", str(tmp_path)).execute({})
        assert res.status.value == "success"
        pkg_dir = tmp_path / "ai-pet-hotel" / "src" / "fusion_ai_pet_hotel"
        assert pkg_dir.is_dir()


class TestOwnerDefault:
    def test_default_owner_is_business_org(self, monkeypatch):
        monkeypatch.delenv("FUSION_BUSINESS_GITHUB_OWNER", raising=False)
        assert business_github_owner() == "dahai80-business"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("FUSION_BUSINESS_GITHUB_OWNER", "acme-corp")
        assert business_github_owner() == "acme-corp"

    def test_repo_client_default_owner(self, monkeypatch):
        monkeypatch.delenv("FUSION_BUSINESS_GITHUB_OWNER", raising=False)
        assert RepoClient(token="t").owner == "dahai80-business"

    def test_repo_client_explicit_owner_wins(self, monkeypatch):
        monkeypatch.setenv("FUSION_BUSINESS_GITHUB_OWNER", "acme-corp")
        assert RepoClient(token="t", owner="legacy").owner == "legacy"

    async def test_ensure_repo_targets_org(self, monkeypatch):
        monkeypatch.delenv("FUSION_BUSINESS_GITHUB_OWNER", raising=False)
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.url.path == "/user":
                return httpx.Response(200, json={"login": "dahai80"})
            if request.method == "GET":
                return httpx.Response(404)
            return httpx.Response(201, json={"full_name": "dahai80-business/ai-pet-hotel"})

        def factory(timeout=15.0):
            return httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))

        info = await RepoClient(token="t", base_url="https://api.github.test", client_factory=factory).ensure_repo(
            "ai-pet-hotel"
        )
        assert info["full_name"] == "dahai80-business/ai-pet-hotel"
        assert seen[0] == ("GET", "/repos/dahai80-business/ai-pet-hotel")
        # issue #418: org owner 建仓走 /orgs/{org}/repos, 不是 /user/repos
        assert ("POST", "/orgs/dahai80-business/repos") in seen
        assert ("POST", "/user/repos") not in seen

    async def test_ensure_repo_personal_owner_uses_user_endpoint(self, monkeypatch):
        monkeypatch.delenv("FUSION_BUSINESS_GITHUB_OWNER", raising=False)
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.url.path == "/user":
                return httpx.Response(200, json={"login": "dahai80"})
            if request.method == "GET":
                return httpx.Response(404)
            return httpx.Response(201, json={"full_name": "dahai80/ai-pet-hotel"})

        def factory(timeout=15.0):
            return httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))

        info = await RepoClient(
            token="t", base_url="https://api.github.test", client_factory=factory, owner="dahai80"
        ).ensure_repo("ai-pet-hotel")
        assert info["full_name"] == "dahai80/ai-pet-hotel"
        assert ("POST", "/user/repos") in seen

    async def test_prepare_intake_plan_repo_uses_org(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FUSION_BUSINESS_GITHUB_OWNER", raising=False)
        monkeypatch.setattr("fusion_cowork.server.intake.run_check_slugs", lambda: {"ran": False, "ok": True})
        registry = tmp_path / "registry.yaml"
        registry.write_text(
            'naming:\n  pattern: "^ai-[a-z0-9]+(-[a-z0-9]+)*$"\n'
            "slugs:\n  ai-zulu: {status: active, source: manual, github: {remote: null}}\nanomalies: []\n"
        )

        class _Stub:
            async def ensure_repo(self, name):
                return {"created": True, "full_name": f"dahai80-business/{name}", "visibility": "private"}

        plan = await prepare_intake("Build a pet hotel booking app now", repo_client=_Stub(), path=registry)
        assert plan["repo"] == "dahai80-business/ai-build-pet-hotel-booking"
