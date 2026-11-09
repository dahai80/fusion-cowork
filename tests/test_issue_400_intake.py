"""issue #400: 交付管线 intake 模式 — requirement 进, slug/私有仓/registry 全自动。

- slug 派生: ASCII 首行取词; 纯中文 → 内容 sha1 (确定性); 冲突 -2/-3 后缀
- 私有仓: GitHub API POST /user/repos, 幂等 (存在 = 复用, cow#361/#368 no-op)
- registry: business/registry.yaml 文本插入 (保注释), source: pipeline + run_id 回填
- check-slugs: 脚本缺失 = 跳过 (warn), rc!=0 = 拒绝建仓
- launch-intake 端点: dry_run 预览 + 显式参数装配 + run 启动 (INTAKE_FACTORY 注入)
"""

import json

import httpx
import pytest

from fusion_cowork.server import intake
from fusion_cowork.server.intake import (
    RepoClient,
    check_slug_slug_valid,
    derive_slug,
    load_registry_slugs,
    next_available_slug,
    prepare_intake,
    register_slug,
    run_check_slugs,
    task_title,
    update_registry_run_id,
)

REGISTRY = """# header comment must survive
naming:
  pattern: "^ai-[a-z0-9]+(-[a-z0-9]+)*$"
slugs:
  ai-alpha: {status: active, source: manual, github: {remote: null}}
  ai-zulu: {status: active, source: manual, github: {remote: null}}
anomalies: []
"""


@pytest.fixture
def registry(tmp_path):
    p = tmp_path / "registry.yaml"
    p.write_text(REGISTRY)
    return p


class TestSlugDerivation:
    def test_ascii_words_from_first_line(self):
        assert derive_slug("Build a Pet Hotel booking app\nwith notifications") == "ai-build-pet-hotel-booking"

    def test_cjk_falls_back_to_deterministic_hash(self):
        req = "做一个宠物寄养预约小程序, 支持门店浏览与微信通知"
        s1 = derive_slug(req)
        assert s1.startswith("ai-")
        assert derive_slug(req) == s1  # same requirement -> same slug

    def test_short_words_dropped(self):
        # words < 2 chars dropped ("a", "I"); single-char-only line -> hash
        assert derive_slug("a I x").startswith("ai-")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            derive_slug("   ")

    def test_pattern_guard(self):
        with pytest.raises(ValueError):
            check_slug_slug_valid("ai-UPPER")
        with pytest.raises(ValueError):
            check_slug_slug_valid("not-a-slug")
        check_slug_slug_valid("ai-pet-hotel")  # valid passes


class TestSlugCollision:
    def test_free_base_returned(self, registry):
        taken = set(load_registry_slugs(registry).keys())
        assert next_available_slug("ai-pet-hotel", taken) == "ai-pet-hotel"

    def test_conflict_suffix(self, registry):
        taken = set(load_registry_slugs(registry).keys())
        assert next_available_slug("ai-alpha", taken) == "ai-alpha-2"
        taken.add("ai-alpha-2")
        assert next_available_slug("ai-alpha", taken) == "ai-alpha-3"


class TestRepoClient:
    def _client(self, handler):
        def factory(timeout=15.0):
            return httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))

        return RepoClient(token="t", base_url="https://api.github.test", client_factory=factory)

    async def test_missing_repo_created_private(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(404)
            body = json.loads(request.content)
            assert body["private"] is True
            return httpx.Response(201, json={"full_name": "dahai80/ai-pet-hotel"})

        info = await self._client(handler).ensure_repo("ai-pet-hotel")
        assert info == {"created": True, "full_name": "dahai80/ai-pet-hotel", "visibility": "private"}
        assert calls[0][0] == "GET"  # check-before-create (idempotent)

    async def test_existing_repo_reused_never_recreated(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"  # no POST ever
            return httpx.Response(200, json={"full_name": "dahai80/ai-alpha", "private": True})

        info = await self._client(handler).ensure_repo("ai-alpha")
        assert info["created"] is False
        assert info["full_name"] == "dahai80/ai-alpha"

    async def test_create_failure_raises_loud(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, text="name already exists on account")

        with pytest.raises(RuntimeError) as ei:
            await self._client(handler).ensure_repo("ai-alpha")
        assert "422" in str(ei.value)

    async def test_invalid_slug_refused_before_api(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("API must not be called for an invalid slug")

        with pytest.raises(ValueError):
            await self._client(handler).ensure_repo("Bad_Slug")


class TestRegistry:
    def test_register_inserts_sorted_with_comments_kept(self, registry):
        info = register_slug("ai-mike", "run_abc", "dahai80/ai-mike", path=registry)
        assert info["registered"] is True
        text = registry.read_text()
        assert "# header comment must survive" in text
        assert "source: pipeline" in text and "run_id: run_abc" in text
        assert "visibility: private" in text
        # alphabetical: ai-alpha < ai-mike < ai-zulu
        assert text.index("ai-alpha") < text.index("ai-mike") < text.index("ai-zulu")

    def test_register_idempotent(self, registry):
        register_slug("ai-alpha", "run_1", "dahai80/ai-alpha", path=registry)
        info = register_slug("ai-alpha", "run_2", "dahai80/ai-alpha", path=registry)
        assert info["existed"] is True and info["registered"] is False
        assert registry.read_text().count("ai-alpha:") == 1

    def test_update_run_id_backfill(self, registry):
        register_slug("ai-mike", "pending", "dahai80/ai-mike", path=registry)
        assert update_registry_run_id("ai-mike", "run_999", path=registry) is True
        assert "run_id: run_999" in registry.read_text()

    def test_update_run_id_unknown_slug_false(self, registry):
        assert update_registry_run_id("ai-ghost", "run_x", path=registry) is False

    def test_missing_slugs_block_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("naming: {}\n")
        with pytest.raises(ValueError):
            register_slug("ai-x", "run", "dahai80/ai-x", path=p)


class TestCheckSlugs:
    def test_missing_script_skips_with_ok(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))  # no ~/fusion/scripts/check-slugs.py there
        res = run_check_slugs(script_path=None)
        assert res["ran"] is False and res["ok"] is True

    def test_failing_script_blocks(self, tmp_path):
        script = tmp_path / "check-slugs.py"
        script.write_text("print('3 errors')\nraise SystemExit(1)\n")
        res = run_check_slugs(script_path=script)
        assert res["ran"] is True and res["ok"] is False and "3 errors" in res["output"]

    def test_passing_script_ok(self, tmp_path):
        script = tmp_path / "check-slugs.py"
        script.write_text("print('0 errors')\n")
        assert run_check_slugs(script_path=script)["ok"] is True


class TestPrepareIntake:
    async def test_dry_run_derives_plan_without_side_effects(self, registry, monkeypatch):
        monkeypatch.setattr(intake, "run_check_slugs", lambda: {"ran": False, "ok": True})
        # no repo client passed -> none needed in dry_run
        plan = await prepare_intake(
            "Build a pet hotel booking app with stores and notifications",
            path=registry,
            dry_run=True,
        )
        assert plan["slug"] == "ai-build-pet-hotel-booking"
        assert plan["repo"] == "dahai80/ai-build-pet-hotel-booking"
        assert plan["dry_run"] is True
        assert "repo_info" not in plan
        # registry untouched
        assert "ai-build-pet-hotel-booking" not in registry.read_text()

    async def test_conflict_auto_suffix_and_registration(self, registry, monkeypatch):
        monkeypatch.setattr(intake, "run_check_slugs", lambda: {"ran": False, "ok": True})
        created = []

        class _Stub:
            async def ensure_repo(self, name):
                created.append(name)
                return {"created": True, "full_name": f"dahai80/{name}", "visibility": "private"}

        plan = await prepare_intake(
            "requirements for the alpha project rewrite",
            repo_client=_Stub(),
            path=registry,
        )
        # "requirements for the alpha project rewrite" -> ai-requirements-alpha-project...
        assert plan["slug"] != "ai-alpha" or plan["suggested_from"] == "ai-alpha"
        assert plan["slug"].startswith("ai-")
        assert created == [plan["slug"]]
        assert f"{plan['slug']}" in registry.read_text()
        assert "run_id: pending" in registry.read_text()

    async def test_too_short_requirement_rejected(self, registry):
        with pytest.raises(ValueError):
            await prepare_intake("short", path=registry, dry_run=True)

    async def test_check_slugs_failure_blocks_repo_creation(self, registry, monkeypatch):
        monkeypatch.setattr(intake, "run_check_slugs", lambda **kw: {"ran": True, "ok": False, "output": "2 errors"})

        class _Boom:
            async def ensure_repo(self, name):  # pragma: no cover
                raise AssertionError("repo must not be created when check-slugs fails")

        with pytest.raises(RuntimeError):
            await prepare_intake("Build a pet hotel booking app now", repo_client=_Boom(), path=registry)


class TestEndpoint:
    def _template(self, tmp_path):
        tdir = tmp_path / "templates"
        tdir.mkdir(exist_ok=True)
        (tdir / "intake_tpl.json").write_text(
            json.dumps(
                {
                    "id": "intake_tpl",
                    "name": "Intake Tpl",
                    "description": "intake test template",
                    "category": "delivery",
                    "params": {
                        "type": "object",
                        "properties": {
                            "slug": {"type": "string", "default": "demo"},
                            "repo": {"type": "string", "default": "org/repo"},
                            "task_title": {"type": "string", "default": "t"},
                        },
                    },
                    "workflow": {
                        "id": "wf_intake",
                        "name": "intake-wf",
                        "params": {"type": "object", "properties": {}},
                        "nodes": [
                            {
                                "id": "n1",
                                "name": "file_input",
                                "config": {"params": {"path": "${param.slug}"}},
                            }
                        ],
                        "edges": [],
                    },
                }
            )
        )
        return tdir

    def _app(self, tmp_path, monkeypatch, registry, stub=None):
        from fastapi.testclient import TestClient

        from fusion_cowork.nodes import import_all_nodes
        from fusion_cowork.server.web_api import create_web_app

        import_all_nodes()  # file_input must be registered for wf.validate()
        monkeypatch.setattr(intake, "run_check_slugs", lambda: {"ran": False, "ok": True})
        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "k400")
        monkeypatch.setenv("FUSION_BUSINESS_REGISTRY", str(registry))

        class _StubModule:
            async def prepare_intake(self, requirement, slug_hint="", repo_client=None, path=None, dry_run=False):
                return await prepare_intake(
                    requirement, slug_hint=slug_hint, repo_client=stub, path=path, dry_run=dry_run
                )

            @staticmethod
            def task_title(requirement):
                return task_title(requirement)

            @staticmethod
            def update_registry_run_id(slug, run_id, path=None):
                return update_registry_run_id(slug, run_id, path=path)

            @staticmethod
            def registry_path():
                return registry

        monkeypatch.setattr(intake, "INTAKE_FACTORY", lambda: _StubModule())
        tdir = self._template(tmp_path)
        app = create_web_app(store_root=tmp_path / "store", templates_dir=tdir)
        return TestClient(app)

    def test_dry_run_previews_slug_and_repo(self, tmp_path, monkeypatch, registry):
        client = self._app(tmp_path, monkeypatch, registry)
        resp = client.post(
            "/api/templates/intake_tpl/launch-intake",
            headers={"Authorization": "Bearer k400"},
            json={"requirement": "Build a pet hotel booking app with stores and notifications", "dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "dry_run"
        assert body["plan"]["slug"] == "ai-build-pet-hotel-booking"
        assert body["plan"]["repo"] == "dahai80/ai-build-pet-hotel-booking"
        assert "ai-build-pet-hotel-booking" not in registry.read_text()

    def test_short_requirement_400(self, tmp_path, monkeypatch, registry):
        client = self._app(tmp_path, monkeypatch, registry)
        resp = client.post(
            "/api/templates/intake_tpl/launch-intake",
            headers={"Authorization": "Bearer k400"},
            json={"requirement": "short"},
        )
        assert resp.status_code == 400

    def test_unknown_template_404(self, tmp_path, monkeypatch, registry):
        client = self._app(tmp_path, monkeypatch, registry)
        resp = client.post(
            "/api/templates/nope/launch-intake",
            headers={"Authorization": "Bearer k400"},
            json={"requirement": "Build a pet hotel booking app now please"},
        )
        assert resp.status_code == 404

    def test_missing_registry_503(self, tmp_path, monkeypatch, tmp_path_factory):
        reg = tmp_path / "absent.yaml"  # never created
        client = self._app(tmp_path, monkeypatch, reg)
        resp = client.post(
            "/api/templates/intake_tpl/launch-intake",
            headers={"Authorization": "Bearer k400"},
            json={"requirement": "Build a pet hotel booking app now please"},
        )
        assert resp.status_code == 503

    def test_unauthorized_401(self, tmp_path, monkeypatch, registry):
        client = self._app(tmp_path, monkeypatch, registry)
        resp = client.post(
            "/api/templates/intake_tpl/launch-intake",
            json={"requirement": "Build a pet hotel booking app now please"},
        )
        assert resp.status_code == 401

    def test_full_launch_starts_run_and_backfills_registry(self, tmp_path, monkeypatch, registry):
        monkeypatch.setattr(intake, "run_check_slugs", lambda: {"ran": False, "ok": True})

        # stub repo client (no network); run start uses the real engine path
        class _Stub:
            async def ensure_repo(self, name):
                return {"created": True, "full_name": f"dahai80/{name}", "visibility": "private"}

        client = self._app(tmp_path, monkeypatch, registry, stub=_Stub())
        resp = client.post(
            "/api/templates/intake_tpl/launch-intake",
            headers={"Authorization": "Bearer k400"},
            json={"requirement": "Build a pet hotel booking app with stores and notifications"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "started" and body["run_id"].startswith("run_")
        assert body["plan"]["slug"] == "ai-build-pet-hotel-booking"
        text = registry.read_text()
        assert "run_id: pending" in text or f"run_id: {body['run_id']}" in text
        assert "source: pipeline" in text
        assert "ai-build-pet-hotel-booking" in text
