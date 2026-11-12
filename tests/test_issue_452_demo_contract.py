"""issue #452: demo_contract — 演示三件套契约门 + bundle 清单 + executor 注册钩子。

离线: 全部文件桩在 tmp_path, executor 注册用 MockTransport, 不触网。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fusion_cowork.nodes import import_all_nodes
from fusion_cowork.nodes.ops.demo_node import DemoContractNode, check_compose, check_demo_yaml, check_stub_pack
from fusion_cowork.templates.template_manager import BUILTIN_TEMPLATES

import_all_nodes()


def _make_good_repo(tmp_path: Path, name="ai-sample") -> Path:
    repo = tmp_path / name
    (repo / "tests" / "fixtures" / "stubs").mkdir(parents=True)
    (repo / "docker-compose.demo.yml").write_text(
        "services:\n  web:\n    image: sample:latest\n    healthcheck:\n      test: curl -f localhost:8080/health\n"
        "  worker:\n    image: worker:latest\n    healthcheck:\n      test: pgrep worker\n",
        encoding="utf-8",
    )
    (repo / "demo.yaml").write_text(
        "web:\n  port: 8080\nterminal: npm run demo\npreset_data: demo/seed.json\n",
        encoding="utf-8",
    )
    (repo / "tests" / "fixtures" / "stubs" / "recorded_flow.json").write_text(
        '{"req": {}, "resp": {"ok": true}}', encoding="utf-8"
    )
    return repo


def _node(**params) -> DemoContractNode:
    node = DemoContractNode()
    node.config.params = params
    return node


class TestContractChecks:
    def test_good_repo_zero_violations(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        assert check_compose(repo) == []
        assert check_demo_yaml(repo) == []
        assert check_stub_pack(repo) == ([], "tests/fixtures/stubs")

    def test_compose_missing(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        (repo / "docker-compose.demo.yml").unlink()
        assert check_compose(repo) == ["missing docker-compose.demo.yml"]

    def test_compose_service_without_healthcheck(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        (repo / "docker-compose.demo.yml").write_text(
            "services:\n  web:\n    image: x\n    healthcheck:\n      test: true\n  db:\n    image: db\n",
            encoding="utf-8",
        )
        errs = check_compose(repo)
        assert errs == ["docker-compose.demo.yml service 'db' missing healthcheck"]

    def test_demo_yaml_needs_entry(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        (repo / "demo.yaml").write_text("preset_data: x\n", encoding="utf-8")
        errs = check_demo_yaml(repo)
        assert errs == ["demo.yaml needs web.port or terminal entry"]

    def test_demo_yaml_mobile_validation(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        (repo / "demo.yaml").write_text("web:\n  port: 80\nmobile:\n  platform: windows\n", encoding="utf-8")
        errs = check_demo_yaml(repo)
        assert any("mobile.platform" in e for e in errs)
        assert any("mobile.artifact" in e for e in errs)
        assert any("mobile.stream" in e for e in errs)

    def test_stub_pack_missing_or_empty(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        errs, d = check_stub_pack(repo)
        assert errs == []
        (repo / "tests" / "fixtures" / "stubs" / "recorded_flow.json").unlink()
        errs, _ = check_stub_pack(repo)
        assert errs and "stub pack missing" in errs[0]


class TestGateExecution:
    async def test_disabled_noop(self, tmp_path):
        res = await _node(enabled=False, deliverable_dir=str(tmp_path)).execute({})
        assert res.data == {"skipped": True}

    async def test_enabled_missing_dir_fails(self, tmp_path):
        with pytest.raises(ValueError, match="not a directory"):
            await _node(enabled=True, deliverable_dir=str(tmp_path / "nope")).execute({})

    async def test_enabled_no_dir_no_root_fails(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FUSION_BUSINESS_ROOT", raising=False)
        with pytest.raises(ValueError, match="FUSION_BUSINESS_ROOT"):
            await _node(enabled=True, slug="sample").execute({})

    async def test_missing_stub_pack_fails_actionable(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        (repo / "tests" / "fixtures" / "stubs" / "recorded_flow.json").unlink()
        with pytest.raises(ValueError) as ei:
            await _node(enabled=True, deliverable_dir=str(repo)).execute({})
        msg = str(ei.value)
        assert "stub pack missing" in msg
        assert str(repo) in msg

    async def test_pass_builds_bundle(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        res = await _node(enabled=True, deliverable_dir=str(repo), slug="sample").execute({})
        assert res.data["passed"] is True
        assert res.data["registered"] is False
        bundle = res.data["bundle"]
        assert bundle["slug"] == "sample"
        assert bundle["services"] == ["web", "worker"]
        assert bundle["entry"]["web_port"] == 8080
        assert bundle["stub_dir"] == "tests/fixtures/stubs"
        assert "recorded_flow.json" in bundle["stub_fixtures"]

    async def test_slug_defaults_to_dirname(self, tmp_path):
        repo = _make_good_repo(tmp_path, name="ai-autoslug")
        res = await _node(enabled=True, deliverable_dir=str(repo)).execute({})
        assert res.data["slug"] == "ai-autoslug"

    async def test_business_root_fallback(self, tmp_path, monkeypatch):
        _make_good_repo(tmp_path, name="ai-rooted")
        monkeypatch.setenv("FUSION_BUSINESS_ROOT", str(tmp_path))
        res = await _node(enabled=True, slug="ai-rooted").execute({})
        assert res.data["passed"] is True

    async def test_secret_scan_blocks_stub_pack(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        stub = repo / "tests" / "fixtures" / "stubs" / "leak.json"
        stub.write_text('{"authorization": "Bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"}', encoding="utf-8")
        with pytest.raises(ValueError, match=r"secret-scan hit"):
            await _node(enabled=True, deliverable_dir=str(repo)).execute({})

    async def test_check_script_own_violation_fails(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        script = tmp_path / "gate.py"
        script.write_text(
            "import sys\n"
            f"sys.stderr.write('ERROR demo-runnable violation: {repo.name}: missing docker-compose.demo.yml\\n')\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=r"missing docker-compose\.demo\.yml"):
            await _node(enabled=True, deliverable_dir=str(repo), check_script=str(script)).execute({})

    async def test_check_script_other_repo_violation_warns_only(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        script = tmp_path / "gate.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write('ERROR demo-runnable violation: some-other-repo: missing stub pack\\n')\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        res = await _node(enabled=True, deliverable_dir=str(repo), check_script=str(script)).execute({})
        assert res.data["bundle"]["check_script"]["other_repo_violations_only"] is True

    async def test_check_script_missing_file_skips(self, tmp_path):
        repo = _make_good_repo(tmp_path)
        res = await _node(enabled=True, deliverable_dir=str(repo), check_script=str(tmp_path / "nope.py")).execute({})
        assert res.data["bundle"]["check_script"]["ran"] is False


class TestRegister:
    async def test_register_true_without_executor_fails(self, tmp_path, monkeypatch):
        repo = _make_good_repo(tmp_path)
        monkeypatch.delenv("FUSION_EXECUTOR_URL", raising=False)
        with pytest.raises(ValueError, match="executor_url"):
            await _node(enabled=True, deliverable_dir=str(repo), register=True).execute({})

    async def test_register_posts_bundle(self, tmp_path, monkeypatch):
        repo = _make_good_repo(tmp_path)
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(202, json={"registered": True})

        async def fake_post(self, url, **kw):
            req = httpx.Request("POST", url, json=kw.get("json"))
            return handler(req)

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        res = await _node(
            enabled=True, deliverable_dir=str(repo), slug="sample", register=True, executor_url="http://127.0.0.1:19999"
        ).execute({})
        assert res.data["registered"] is True
        assert captured["url"].endswith("/api/v1/demo/register")
        assert captured["body"]["slug"] == "sample"
        assert captured["body"]["kind"] == "web"
        assert captured["body"]["bundle"]["entry"]["web_port"] == 8080

    async def test_register_http_error_fails_loud(self, tmp_path):
        repo = _make_good_repo(tmp_path)

        async def fake_post(self, url, **kw):
            return httpx.Response(500, text="boom", request=httpx.Request("POST", url))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("httpx.AsyncClient.post", fake_post)
            with pytest.raises(ValueError, match=r"demo register to http://127\.0\.0\.1:19999 failed: 500"):
                await _node(
                    enabled=True, deliverable_dir=str(repo), register=True, executor_url="http://127.0.0.1:19999"
                ).execute({})


class TestTemplateWiring:
    def test_template_has_demo_gate_node(self):
        tpl = next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")
        n13 = next(n for n in tpl["workflow"]["nodes"] if n["id"] == "n13")
        assert n13["name"] == "demo_contract"
        p = n13["config"]["params"]
        assert p["enabled"] == "${param.demo_gate}"
        assert p["deliverable_dir"] == "${param.demo_deliverable_dir}"
        assert p["slug"] == "${param.slug}"
        assert p["register"] == "${param.demo_register}"
        assert ("n12", "n13") in {(e["source_id"], e["target_id"]) for e in tpl["workflow"]["edges"]}

    def test_template_params_default_off(self):
        tpl = next(t for t in BUILTIN_TEMPLATES if t["id"] == "product_lifecycle")
        assert tpl["params"]["properties"]["demo_gate"]["default"] is False
        assert tpl["params"]["properties"]["demo_register"]["default"] is False

    def test_node_registered_with_alias(self):
        from fusion_cowork.engine.node import NodeRegistry

        assert NodeRegistry.get("demo_contract") is DemoContractNode
        from fusion_cowork import NODE_NAME_ALIASES

        assert NODE_NAME_ALIASES["演示契约"] == "demo_contract"
