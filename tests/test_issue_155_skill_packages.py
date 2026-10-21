"""issue #155: markdown skill 包 — frontmatter 解析 / 发现 / 参数表单 / 执行。"""

from __future__ import annotations

import json

import pytest


def test_parse_markdown_package():
    from fusion_cowork.skills.packages import parse_markdown

    text = """---
name: demoSKill
title: 演示
category: demo
aliases: [演示技能]
handler: demo_handler
params:
  - name: count
    type: int
    default: 3
  - name: note
    type: string
    required: true
---

执行演示指令。
"""
    pkg = parse_markdown(text)
    assert pkg.name == "/demoSKill"
    assert pkg.title == "演示"
    assert [p.name for p in pkg.params] == ["count", "note"]
    assert pkg.instructions == "执行演示指令。"


def test_parse_markdown_rejects_bad_frontmatter():
    from fusion_cowork.skills.packages import parse_markdown

    with pytest.raises(ValueError):
        parse_markdown("no frontmatter")
    with pytest.raises(ValueError):
        parse_markdown("---\ntitle: x\n---\nbody")
    with pytest.raises(ValueError):
        parse_markdown("---\nname: x\n")


def test_builtin_seed_packages_discovered():
    from fusion_cowork.skills.packages import get_package_registry, reset_package_registry

    reset_package_registry()
    registry = get_package_registry()
    names = {p.name for p in registry.list_packages()}
    assert {"/contract-review", "/spreadsheet-recon", "/weekly-readout"} <= names
    contract = registry.get("/contract-review")
    assert contract is not None
    assert contract.output_template
    # 别名同样可寻址
    assert registry.get("合同审查") is contract


def test_param_coercion_and_required_errors():
    from fusion_cowork.skills.packages import BUILTIN_PACKAGES_DIR, load_package

    pkg = load_package(BUILTIN_PACKAGES_DIR / "weekly-readout.md")
    clean, errors = pkg.coerce({"days": "14", "narrative": "true"})
    assert errors == []
    assert clean["days"] == 14
    assert clean["narrative"] is True

    _, errors = pkg.coerce({"days": "not-a-number"})
    assert any("days" in e for e in errors)

    contract = load_package(BUILTIN_PACKAGES_DIR / "contract-review.md")
    _, errors = contract.coerce({})
    assert any("contract_path" in e for e in errors)


def test_unknown_package_run():
    import asyncio

    from fusion_cowork.skills.packages import get_package_registry, reset_package_registry

    reset_package_registry()
    result = asyncio.run(get_package_registry().run("/nope", {}))
    assert "未知 skill 包" in result["error"]


class _FakeLLM:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def chat(self, model, messages, **kwargs):
        _FakeLLM.calls.append({"model": model, "messages": messages})

        class _Resp:
            content = "## 风险条款\n| 条款 | 依据 |\n|---|---|\n| 付款 | [P1] |"

        return _Resp()


def test_contract_review_runs_local_with_fake_llm(tmp_path, monkeypatch):
    import asyncio

    from fusion_cowork.ai import mlx_client

    monkeypatch.setattr(mlx_client, "FusionMLXClient", _FakeLLM)
    contract = tmp_path / "contract.md"
    contract.write_text("甲方应于交付后三十日内付款。", encoding="utf-8")

    from fusion_cowork.skills.packages import get_package_registry, reset_package_registry

    reset_package_registry()
    result = asyncio.run(
        get_package_registry().run(
            "/contract-review",
            {"contract_path": str(contract), "output_dir": str(tmp_path)},
        )
    )
    assert result["status"] == "success", result
    memo = tmp_path / "contract.memo.md"
    assert memo.is_file()
    assert "合同审查备忘" in memo.read_text(encoding="utf-8")
    assert _FakeLLM.calls and "三十日" in _FakeLLM.calls[-1]["messages"][1]["content"]


def test_weekly_readout_aggregates_lifecycle(tmp_path):
    import asyncio
    import time

    src = tmp_path / "lifecycle"
    src.mkdir()
    now = time.time()
    lines = [
        {"seq": 1, "ts": now - 60, "domain": "workflow", "event": "run_end", "payload": {"status": "success"}},
        {"seq": 2, "ts": now - 30, "domain": "workflow", "event": "run_failed", "payload": {"status": "failed"}},
        {"seq": 3, "ts": now - 86400 * 30, "domain": "workflow", "event": "run_end", "payload": {}},
    ]
    (src / "global.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    from fusion_cowork.skills.packages import get_package_registry, reset_package_registry

    reset_package_registry()
    out = tmp_path / "reports"
    result = asyncio.run(
        get_package_registry().run(
            "/weekly-readout",
            {"days": 7, "source_dir": str(src), "output_dir": str(out)},
        )
    )
    assert result["status"] == "success", result
    assert result["data"]["events"] == 2
    assert result["data"]["failures"] == 1
    report = next(out.glob("weekly-readout-*.md"))
    body = report.read_text(encoding="utf-8")
    assert "事件总数: 2" in body
    assert "run_failed" in body


class TestSkillsApi:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_cowork.engine import WorkflowEngine
        from fusion_cowork.server import web_api

        monkeypatch.setenv("FUSION_COWORK_WEB_API_KEY", "fc-test-155")
        web_api._WEB_DIR = tmp_path / "web"
        app = web_api.create_web_app(
            engine=WorkflowEngine(),
            dist_dir=tmp_path / "no-dist",
            store_root=tmp_path / "workflows",
        )
        return TestClient(app, headers={"x-api-key": "fc-test-155"})

    def test_list_and_detail(self, client):
        listing = client.get("/api/skills")
        assert listing.status_code == 200, listing.text
        names = {s["name"] for s in listing.json()["skills"]}
        assert "/contract-review" in names
        assert "/weekly-readout" in names

        detail = client.get("/api/skills/%2Fweekly-readout")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["params"] and body["params"][0]["name"] == "days"

        missing = client.get("/api/skills/%2Fnope")
        assert missing.status_code == 404

    def test_run_weekly_readout(self, client, tmp_path):
        src = tmp_path / "lc"
        src.mkdir()
        (src / "s.jsonl").write_text(
            json.dumps({"seq": 1, "ts": __import__("time").time(), "domain": "workflow", "event": "run_end"}) + "\n",
            encoding="utf-8",
        )
        resp = client.post(
            "/api/skills/%2Fweekly-readout/run",
            json={"params": {"days": 7, "source_dir": str(src), "output_dir": str(tmp_path / "out")}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["events"] == 1

    def test_run_rejects_missing_required(self, client):
        resp = client.post("/api/skills/%2Fcontract-review/run", json={"params": {}})
        assert resp.status_code == 400
        assert resp.json()["errors"]

    def test_run_unknown_package(self, client):
        resp = client.post("/api/skills/%2Fnope/run", json={"params": {}})
        assert resp.status_code == 404
